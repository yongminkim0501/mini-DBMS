"""PostgreSQL 프론트엔드/백엔드 프로토콜 v3 메시지 인코딩/디코딩 (모든 정수는 빅엔디언).

프레임 구조::

    시작 단계:   int32 length | int32 code | payload            (length 는 자기 자신 포함, 타입 바이트 없음)
    그 이후:     byte1 type   | int32 length | payload           (length 는 자기 자신 포함, 타입 바이트 제외)

이 모듈은 바이트만 다룬다 (소켓, 상태 없음). 모듈 수준에는 컴파일된 ``struct.Struct`` 와 불변 상수만 있다.
길이 검사는 PostgreSQL 과 같은 방식이다: 시작 패킷은 10,000 바이트, 짧아야 하는 메시지(Sync/Flush/Terminate/
Describe/Close/Execute)는 10,000 바이트, 나머지(Query/Parse/Bind)는 1GB 미만. 넘으면 08P01 (연결을 끊는다).
"""

from __future__ import annotations

import struct
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from ..errors import DatabaseError, ProtocolViolation
from ..result import BIGINT_OID, ColumnInfo

__all__ = [
    "SSL_REQUEST_CODE",
    "GSSENC_REQUEST_CODE",
    "CANCEL_REQUEST_CODE",
    "PROTOCOL_3_0",
    "MAX_STARTUP_PACKET",
    "MAX_MESSAGE_BYTES",
    "StartupPacket",
    "take_startup_packet",
    "peek_startup_packet",
    "has_complete_message",
    "parse_startup_params",
    "take_message",
    "Reader",
    "auth_ok",
    "parameter_status",
    "backend_key_data",
    "ready_for_query",
    "negotiate_protocol_version",
    "row_description",
    "encode_row",
    "command_complete",
    "empty_query_response",
    "error_response",
    "parse_complete",
    "bind_complete",
    "close_complete",
    "no_data",
    "portal_suspended",
    "parameter_description",
    "type_length",
]

SSL_REQUEST_CODE = 80877103  # (1234 << 16) | 5679
GSSENC_REQUEST_CODE = 80877104  # (1234 << 16) | 5680
CANCEL_REQUEST_CODE = 80877102  # (1234 << 16) | 5678
PROTOCOL_3_0 = 3 << 16

#: 시작 패킷 최대 길이 (PostgreSQL MAX_STARTUP_PACKET_LENGTH)
MAX_STARTUP_PACKET = 10_000
#: 일반 메시지 최대 길이 (PostgreSQL PQ_LARGE_MESSAGE_LIMIT = 1GB - 1)
MAX_MESSAGE_BYTES = 0x3FFFFFFF
#: 짧아야 하는 메시지의 최대 길이 (PostgreSQL PQ_SMALL_MESSAGE_LIMIT)
MAX_SMALL_MESSAGE = 10_000
_SMALL_TYPES = frozenset(b"SHXDCE")

_I16 = struct.Struct("!h")
_U16 = struct.Struct("!H")
_I32 = struct.Struct("!i")
_U32 = struct.Struct("!I")
_I64 = struct.Struct("!q")
_HEADER = struct.Struct("!ci")  # 타입 바이트 + 길이
_LEN_CODE = struct.Struct("!iI")  # 시작 패킷: 길이 + 코드
_NULL_FIELD = _I32.pack(-1)


def _msg(kind: bytes, body: bytes = b"") -> bytes:
    return kind + _I32.pack(len(body) + 4) + body


def _cstr(s: str) -> bytes:
    return s.encode("utf-8") + b"\x00"


# ------------------------------------------------------------------ 프론트엔드 → 백엔드 (디코딩)


@dataclass(frozen=True, slots=True)
class StartupPacket:
    """시작 단계 패킷 하나. ``code`` 는 프로토콜 버전 또는 SSL/GSSENC/Cancel 요청 코드, ``payload`` 는 그 뒤 바이트."""

    code: int
    payload: bytes


def take_startup_packet(buf: bytearray, *, max_length: int = MAX_STARTUP_PACKET) -> StartupPacket | None:
    """버퍼 앞의 완전한 시작 패킷을 꺼낸다 (꺼낸 만큼 버퍼에서 지운다). 아직 다 오지 않았으면 None.

    Raises:
        ProtocolViolation: 길이가 8 미만이거나 ``max_length`` 초과 (``invalid length of startup packet``, FATAL).
    """
    if len(buf) < 4:
        return None
    (length,) = _I32.unpack_from(buf, 0)
    if length < 8 or length > max_length:
        raise ProtocolViolation("invalid length of startup packet", severity="FATAL")
    if len(buf) < length:
        return None
    (_, code) = _LEN_CODE.unpack_from(buf, 0)
    payload = bytes(buf[8:length])
    del buf[:length]
    return StartupPacket(code, payload)


def peek_startup_packet(buf: bytearray | bytes, *, max_length: int = MAX_STARTUP_PACKET) -> int | None:
    """버퍼 앞의 시작 패킷이 완전하면 그 코드를, 아니면 None (꺼내지 않는다). 길이가 잘못되면 ProtocolViolation."""
    if len(buf) < 4:
        return None
    (length,) = _I32.unpack_from(buf, 0)
    if length < 8 or length > max_length:
        raise ProtocolViolation("invalid length of startup packet", severity="FATAL")
    if len(buf) < length:
        return None
    return _LEN_CODE.unpack_from(buf, 0)[1]


def parse_startup_params(payload: bytes) -> dict[str, str]:
    """StartupMessage 의 ``name\\0value\\0 ... \\0`` 를 dict 로.

    Raises:
        ProtocolViolation: 마지막 바이트가 종료 표시(0)가 아니거나 이름/값 짝이 맞지 않음, UTF-8 이 아님 (FATAL).
    """
    if not payload or payload[-1] != 0:
        raise ProtocolViolation("invalid startup packet layout: expected terminator as last byte", severity="FATAL")
    parts = payload[:-1].split(b"\x00")
    if parts == [b""]:
        return {}
    if parts[-1] != b"" or len(parts) % 2 != 1:
        raise ProtocolViolation("invalid startup packet layout: expected terminator as last byte", severity="FATAL")
    parts = parts[:-1]
    try:
        items = [p.decode("utf-8") for p in parts]
    except UnicodeDecodeError:
        raise ProtocolViolation("invalid startup packet: not valid UTF-8", severity="FATAL") from None
    out: dict[str, str] = {}
    for i in range(0, len(items), 2):
        out[items[i]] = items[i + 1]
    return out


def take_message(buf: bytearray, *, max_length: int = MAX_MESSAGE_BYTES) -> tuple[int, bytes] | None:
    """버퍼 앞의 완전한 메시지 ``(타입 바이트, 본문)`` 을 꺼낸다. 아직 다 오지 않았으면 None (버퍼는 그대로).

    Raises:
        ProtocolViolation: 길이 필드가 4 미만이거나 타입별 최대 길이 초과 (``invalid message length``, FATAL).
    """
    if len(buf) < 5:
        return None
    kind = buf[0]
    (length,) = _I32.unpack_from(buf, 1)
    limit = MAX_SMALL_MESSAGE if kind in _SMALL_TYPES else max_length
    if length < 4 or length > limit:
        raise ProtocolViolation(f"invalid message length {length} for message type \"{chr(kind)}\"", severity="FATAL")
    total = length + 1
    if len(buf) < total:
        return None
    body = bytes(buf[5:total])
    del buf[:total]
    return kind, body


def has_complete_message(buf: bytearray | bytes, *, max_length: int = MAX_MESSAGE_BYTES) -> bool:
    """버퍼 앞에 완전한 메시지가 하나 이상 있는가 (꺼내지 않는다). 길이가 잘못되면 ProtocolViolation."""
    if len(buf) < 5:
        return False
    kind = buf[0]
    (length,) = _I32.unpack_from(buf, 1)
    limit = MAX_SMALL_MESSAGE if kind in _SMALL_TYPES else max_length
    if length < 4 or length > limit:
        raise ProtocolViolation(f"invalid message length {length} for message type \"{chr(kind)}\"", severity="FATAL")
    return len(buf) >= length + 1


class Reader:
    """메시지 본문 읽기 도우미. 모자라거나 남는 바이트는 08P01 (``invalid message format``)."""

    __slots__ = ("data", "pos")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def _need(self, n: int) -> None:
        if n < 0 or self.pos + n > len(self.data):
            raise ProtocolViolation("invalid message format")

    def int16(self) -> int:
        self._need(2)
        (v,) = _I16.unpack_from(self.data, self.pos)
        self.pos += 2
        return v

    def uint16(self) -> int:
        self._need(2)
        (v,) = _U16.unpack_from(self.data, self.pos)
        self.pos += 2
        return v

    def int32(self) -> int:
        self._need(4)
        (v,) = _I32.unpack_from(self.data, self.pos)
        self.pos += 4
        return v

    def uint32(self) -> int:
        self._need(4)
        (v,) = _U32.unpack_from(self.data, self.pos)
        self.pos += 4
        return v

    def byte(self) -> int:
        self._need(1)
        v = self.data[self.pos]
        self.pos += 1
        return v

    def bytes(self, n: int) -> bytes:
        self._need(n)
        v = self.data[self.pos : self.pos + n]
        self.pos += n
        return v

    def cstring(self) -> str:
        end = self.data.find(b"\x00", self.pos)
        if end < 0:
            raise ProtocolViolation("invalid string in message")
        raw = self.data[self.pos : end]
        self.pos = end + 1
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ProtocolViolation("invalid string in message: not valid UTF-8") from None

    def end(self) -> None:
        """본문을 모두 읽었는지 확인한다."""
        if self.pos != len(self.data):
            raise ProtocolViolation("invalid message format")


# ------------------------------------------------------------------ 백엔드 → 프론트엔드 (인코딩)


def auth_ok() -> bytes:
    """AuthenticationOk ('R', 0). trust 인증: 비밀번호를 묻지 않는다."""
    return _msg(b"R", _I32.pack(0))


def parameter_status(name: str, value: str) -> bytes:
    """ParameterStatus ('S')."""
    return _msg(b"S", _cstr(name) + _cstr(value))


def backend_key_data(process_id: int, secret: int) -> bytes:
    """BackendKeyData ('K'): 취소 요청에 쓸 (프로세스 ID, 비밀 키). 둘 다 32비트 부호 없는 값으로 보낸다."""
    return _msg(b"K", _U32.pack(process_id) + _U32.pack(secret))


def ready_for_query(status: bytes = b"I") -> bytes:
    """ReadyForQuery ('Z'). 트랜잭션이 없으므로 상태는 항상 'I' (idle)."""
    return _msg(b"Z", status)


def negotiate_protocol_version(newest_minor: int, unsupported_options: Sequence[str]) -> bytes:
    """NegotiateProtocolVersion ('v'): 지원하는 가장 높은 minor 버전과 인식하지 못한 ``_pq_.`` 옵션 목록."""
    body = _I32.pack(newest_minor) + _I32.pack(len(unsupported_options))
    body += b"".join(_cstr(o) for o in unsupported_options)
    return _msg(b"v", body)


def type_length(type_oid: int) -> int:
    """pg_type.typlen: int8 은 8, text 는 가변 길이(-1)."""
    return 8 if type_oid == BIGINT_OID else -1


def row_description(columns: Sequence[ColumnInfo], formats: Sequence[int] | None = None) -> bytes:
    """RowDescription ('T'): 컬럼마다 이름, 테이블 OID, 컬럼 번호, 타입 OID, 타입 길이, typmod(-1), 형식 코드."""
    parts = [_I16.pack(len(columns))]
    for i, c in enumerate(columns):
        fmt = formats[i] if formats else 0
        parts.append(
            _cstr(c.name)
            + _U32.pack(c.table_oid)
            + _I16.pack(c.column_number)
            + _U32.pack(c.type_oid)
            + _I16.pack(type_length(c.type_oid))
            + _I32.pack(-1)
            + _I16.pack(fmt)
        )
    return _msg(b"T", b"".join(parts))


def encode_row(row: Sequence[Any], binary: Sequence[bool] | None = None) -> bytes:
    """DataRow ('D'). 텍스트 형식: 정수는 10진 문자열, 문자열은 UTF-8. NULL 은 길이 -1.

    ``binary[i]`` 가 True 면 그 컬럼은 바이너리 형식 (int8 은 8바이트 빅엔디언 부호 있는 정수, text 는 UTF-8 그대로).
    """
    parts = [_I16.pack(len(row))]
    append = parts.append
    pack32 = _I32.pack
    if binary is None:
        for v in row:
            if v is None:
                append(_NULL_FIELD)
                continue
            b = (v if type(v) is str else str(v)).encode("utf-8")
            append(pack32(len(b)))
            append(b)
    else:
        for v, is_bin in zip(row, binary, strict=True):
            if v is None:
                append(_NULL_FIELD)
                continue
            if is_bin and type(v) is int:
                b = _I64.pack(v)
            else:
                b = (v if type(v) is str else str(v)).encode("utf-8")
            append(pack32(len(b)))
            append(b)
    body = b"".join(parts)
    return b"D" + pack32(len(body) + 4) + body


def command_complete(tag: str) -> bytes:
    """CommandComplete ('C')."""
    return _msg(b"C", _cstr(tag))


def empty_query_response() -> bytes:
    """EmptyQueryResponse ('I'): 빈 쿼리 문자열."""
    return _msg(b"I")


def error_response(err: DatabaseError, *, position: int | None = None) -> bytes:
    """ErrorResponse ('E'). 필드: S(심각도) V(지역화하지 않은 심각도) C(SQLSTATE) M D H P(1부터의 문자 위치).

    ``position`` 을 주면 ``err.position`` 대신 쓴다 (메시지 전체 기준으로 바꾼 위치).
    """
    pos = err.position if position is None else position
    fields: list[tuple[bytes, str | None]] = [
        (b"S", err.severity),
        (b"V", err.severity),
        (b"C", err.sqlstate),
        (b"M", err.message),
        (b"D", err.detail),
        (b"H", err.hint),
        (b"P", str(pos) if pos is not None else None),
    ]
    body = b"".join(code + _cstr(value) for code, value in fields if value is not None) + b"\x00"
    return _msg(b"E", body)


def parse_complete() -> bytes:
    return _msg(b"1")


def bind_complete() -> bytes:
    return _msg(b"2")


def close_complete() -> bytes:
    return _msg(b"3")


def no_data() -> bytes:
    return _msg(b"n")


def portal_suspended() -> bytes:
    return _msg(b"s")


def parameter_description(type_oids: Iterable[int]) -> bytes:
    """ParameterDescription ('t'): 파라미터 수와 각 타입 OID."""
    oids = list(type_oids)
    return _msg(b"t", _I16.pack(len(oids)) + b"".join(_U32.pack(o) for o in oids))
