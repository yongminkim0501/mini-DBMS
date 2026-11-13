"""연결 하나의 PostgreSQL 프로토콜 상태 기계 (시작 단계, 단순 쿼리, 확장 쿼리, 쿼리 취소 플래그).

소켓을 직접 다루지 않는다: 들어온 메시지를 :meth:`PgBackend.handle_message` 에 넘기면 응답 바이트를
:class:`Output` 에 쓴다. 그래서 소켓 없이도 단위 테스트할 수 있고, 서버(:mod:`minidb.pgwire.service`)는
"버퍼에서 완전한 메시지를 꺼내 넘기기"와 "Output 을 소켓으로 내보내기"만 한다.

스레드 규칙: 한 연결(PgBackend)은 한 순간에 한 스레드(워커)만 처리한다 (디스패처의 연결 소유권 규칙).
예외는 쿼리 취소다: 다른 연결(CancelRequest)을 처리하는 디스패처 스레드가 :meth:`PgBackend.request_cancel` 을
부른다. 그래서 "지금 실행 중인가"와 취소 플래그는 ``self._lock`` 아래에서만 바꾼다.

메시지 흐름 (docs/pg-protocol.md 의 시퀀스 다이어그램)::

    단순 쿼리   Q ─▶ [T D* C | C | I | E] (문장마다, 오류에서 멈춤) ─▶ Z
    확장 쿼리   P ─▶ 1    B ─▶ 2    D ─▶ t T|n / T|n    E ─▶ D* (C | s)    C ─▶ 3    H ─▶ (전송)    S ─▶ Z
                오류가 나면 E 를 보내고 Sync 가 올 때까지 이후 메시지를 모두 버린다.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..binder import PARAM_TYPE_FAMILIES, BoundSelect, bind_values, parse_integer_text
from ..catalog import BIGINT
from ..engine import Database, PreparedStatement
from ..errors import (
    CharacterNotInRepertoire,
    DatabaseError,
    DuplicateCursor,
    DuplicatePreparedStatement,
    FeatureNotSupported,
    InvalidAuthorizationSpecification,
    InvalidBinaryRepresentation,
    InvalidCatalogName,
    InvalidCursorName,
    InvalidParameterValue,
    InvalidSqlStatementName,
    ProtocolViolation,
)
from ..result import QueryResult
from ..session import Session, is_utf8_encoding_name
from ..sql.lexer import iter_statements
from . import messages as m

__all__ = ["Output", "PgBackend", "BackendHost", "decode_param", "SUPPORTED_MINOR"]

#: 지원하는 프로토콜 minor 버전 (3.0 만)
SUPPORTED_MINOR = 0
_FLUSH_BYTES = 64 * 1024
_BINARY_SIZES = {20: 8, 23: 4, 21: 2}


class Output:
    """응답 바이트 버퍼. ``threshold`` 를 넘으면 바로 내보낸다 (큰 결과를 메모리에 모으지 않는 스트리밍)."""

    __slots__ = ("_buf", "_send", "threshold", "sent_bytes")

    def __init__(self, send: Callable[[bytes], None], threshold: int = _FLUSH_BYTES) -> None:
        self._buf = bytearray()
        self._send = send
        self.threshold = threshold
        self.sent_bytes = 0

    def write(self, data: bytes) -> None:
        self._buf += data
        if len(self._buf) >= self.threshold:
            self.flush()

    def take(self) -> bytes:
        """보내지 않은 바이트를 꺼낸다 (연결을 닫으며 마지막으로 보낼 때: lingering close 에 넘긴다)."""
        data = bytes(self._buf)
        self._buf.clear()
        return data

    def flush(self) -> None:
        if self._buf:
            data = bytes(self._buf)
            self._buf.clear()
            self._send(data)
            self.sent_bytes += len(data)


class BackendHost(Protocol):
    """PgBackend 가 서버에 기대하는 것 (테스트에서는 간단한 가짜로 바꿔 끼운다)."""

    db: Database
    database_name: str

    def register_backend(self, backend: PgBackend) -> None: ...
    def query_started(self) -> None: ...
    def query_finished(self) -> None: ...
    def log(self, line: str) -> None: ...


@dataclass(slots=True)
class Portal:
    """Bind 결과. 실행 상태(결과 이터레이터와 스냅샷)를 Execute 사이에 유지한다."""

    name: str
    prepared: PreparedStatement
    bound: Any  # 값이 채워진 BoundStatement (빈 쿼리면 None)
    formats: list[int]  # 결과 컬럼별 형식 코드 (0 텍스트, 1 바이너리)
    result: QueryResult | None = None
    rows: Iterator[tuple[Any, ...]] | None = None
    done: bool = False
    binary: list[bool] | None = field(default=None)

    def close(self) -> None:
        close = getattr(self.rows, "close", None)
        if close is not None:
            close()  # 스캔 제너레이터와 파일 핸들 정리
        self.rows = None


def decode_param(raw: bytes | None, fmt: int, type_oid: int, number: int) -> int | str | None:
    """Bind 메시지의 파라미터 값 하나를 파라미터 타입에 맞는 파이썬 값으로.

    텍스트 형식(0): UTF-8 문자열. 정수 계열 타입이면 그 타입의 입력 함수 규칙으로 해석 (22P02/22003).
    바이너리 형식(1): int2/int4/int8 은 빅엔디언 부호 있는 정수 (길이가 다르면 22P03), 문자열 계열은 UTF-8 바이트.
    """
    if raw is None:
        return None
    family = PARAM_TYPE_FAMILIES[type_oid]
    if fmt == 1 and family == BIGINT:
        if len(raw) != _BINARY_SIZES[type_oid]:
            raise InvalidBinaryRepresentation(f"incorrect binary data format in bind parameter {number}")
        return int.from_bytes(raw, "big", signed=True)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        bad = raw[exc.start : exc.start + 1].hex()
        raise CharacterNotInRepertoire(f'invalid byte sequence for encoding "UTF8": 0x{bad}') from None
    if family == BIGINT:
        return parse_integer_text(text, type_oid)
    return text


class PgBackend:
    """연결 하나의 프로토콜 상태.

    Args:
        host: 서버 (DB, 데이터베이스 이름, 취소 등록, 지표, 로그).
        backend_id: BackendKeyData 의 프로세스 ID 자리에 보낼 연결 번호.
        peer: 원격 주소 (접속 로그용).
    """

    def __init__(self, host: BackendHost, backend_id: int, peer: str = "") -> None:
        self.host = host
        self.db = host.db
        self.id = backend_id
        self.secret = secrets.randbits(32)  # 취소 요청의 비밀 키 (추측 불가)
        self.peer = peer
        self.connected_at = time.monotonic()
        self.started = False
        self.user = ""
        self.database = ""
        self.application_name = ""
        self.session: Session | None = None
        self.statements: dict[str, PreparedStatement] = {}
        self.portals: dict[str, Portal] = {}
        self.skip_until_sync = False
        self.queries = 0
        # 아래 둘은 self._lock 아래에서만 바꾼다 (취소 요청은 다른 스레드에서 온다)
        self._lock = threading.Lock()
        self._running = False
        self.cancel_event = threading.Event()

    # ------------------------------------------------------------ 취소
    def _begin(self) -> None:
        with self._lock:
            self._running = True
            self.cancel_event.clear()  # 이전 문장에 대한 늦은 취소가 새 문장을 죽이지 않게
        self.host.query_started()

    def _end(self) -> None:
        with self._lock:
            self._running = False
            self.cancel_event.clear()
        self.host.query_finished()

    def request_cancel(self) -> bool:
        """다른 스레드(디스패처)에서: 실행 중인 문장이 있으면 취소 플래그를 켠다. 켰으면 True.

        실행 중이 아니면(유휴, 문장 사이) 아무 일도 하지 않는다 — PostgreSQL 과 같다.
        """
        with self._lock:
            if self._running:
                self.cancel_event.set()
                return True
            return False

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    # ------------------------------------------------------------ 시작 단계
    def handle_startup(self, params: dict[str, str], version: int, out: Output) -> bool:
        """StartupMessage 처리. 성공하면 AuthenticationOk … ReadyForQuery 를 쓰고 True, FATAL 이면 오류를 쓰고 False."""
        major, minor = version >> 16, version & 0xFFFF
        try:
            if major != 3:
                raise FeatureNotSupported(
                    f"unsupported frontend protocol {major}.{minor}: server supports 3.0 to 3.{SUPPORTED_MINOR}",
                    severity="FATAL",
                )
            unknown_pq = [k for k in params if k.startswith("_pq_.")]
            user = params.get("user", "")
            if not user:
                raise InvalidAuthorizationSpecification(
                    "no PostgreSQL user name specified in startup packet", severity="FATAL"
                )
            database = params.get("database") or user
            if database != self.host.database_name:
                raise InvalidCatalogName(f'database "{database}" does not exist', severity="FATAL")
            enc = params.get("client_encoding")
            if enc is not None and not is_utf8_encoding_name(enc):
                raise InvalidParameterValue(
                    f'invalid value for parameter "client_encoding": "{enc}"', severity="FATAL"
                )
        except DatabaseError as exc:
            out.write(m.error_response(exc))
            return False

        if minor > SUPPORTED_MINOR or unknown_pq:
            out.write(m.negotiate_protocol_version(SUPPORTED_MINOR, unknown_pq))
        self.user = user
        self.database = database
        self.application_name = params.get("application_name", "")
        self.session = Session(application_name=self.application_name, session_authorization=user)
        self.session.cancel = self.cancel_event
        out.write(m.auth_ok())  # trust 인증 (docs/pg-protocol.md: 인증 없음, 외부 노출 금지)
        for name, value in self.session.reported_values():
            out.write(m.parameter_status(name, value))
        out.write(m.backend_key_data(self.id, self.secret))
        out.write(m.ready_for_query())
        self.started = True
        self.host.register_backend(self)
        return True

    # ------------------------------------------------------------ 메시지 분기
    def handle_message(self, kind: int, body: bytes, out: Output) -> bool:
        """시작 이후의 메시지 하나를 처리한다. 연결을 끊어야 하면 False (Terminate, FATAL)."""
        if kind == 0x58:  # 'X' Terminate
            return False
        if self.skip_until_sync and kind != 0x53:
            return True  # 확장 쿼리 오류 이후: Sync 까지 모두 버린다 (PostgreSQL 규칙)
        try:
            if kind == 0x51:  # 'Q'
                self._simple_query(m.Reader(body), out)
            elif kind == 0x53:  # 'S' Sync
                self._sync(out)
            elif kind == 0x48:  # 'H' Flush
                out.flush()
            elif kind == 0x50:  # 'P'
                self._parse(m.Reader(body), out)
            elif kind == 0x42:  # 'B'
                self._bind(m.Reader(body), out)
            elif kind == 0x44:  # 'D'
                self._describe(m.Reader(body), out)
            elif kind == 0x45:  # 'E'
                self._execute(m.Reader(body), out)
            elif kind == 0x43:  # 'C'
                self._close(m.Reader(body), out)
            elif kind == 0x46:  # 'F' FunctionCall: 범위 밖. 오류 후 ReadyForQuery (PostgreSQL 의 F 는 Sync 없이 끝난다)
                out.write(m.error_response(FeatureNotSupported("function call messages are not supported")))
                out.write(m.ready_for_query())
            else:
                raise ProtocolViolation(f"invalid frontend message type {kind}", severity="FATAL")
        except DatabaseError as exc:
            out.write(m.error_response(exc))
            if exc.severity == "FATAL":
                return False
            if kind == 0x51:
                out.write(m.ready_for_query())  # 잘못된 Q 메시지: 단순 쿼리는 Sync 없이 끝난다
            else:
                self.skip_until_sync = True
        except Exception as exc:  # noqa: BLE001 - 엔진 버그: 연결은 살리고 XX000 으로 알린다
            self.host.log(f"pg backend {self.id}: internal error: {exc!r}")
            out.write(m.error_response(DatabaseError(f"internal error: {type(exc).__name__}: {exc}")))
            if kind == 0x51:
                out.write(m.ready_for_query())
            else:
                self.skip_until_sync = True
        return True

    def _report_changes(self, out: Output) -> None:
        assert self.session is not None
        for name, value in self.session.take_reported_changes():
            out.write(m.parameter_status(name, value))

    # ------------------------------------------------------------ 단순 쿼리
    def _simple_query(self, r: m.Reader, out: Output) -> None:
        sql = r.cstring()
        r.end()
        self.queries += 1
        # 단순 쿼리는 이름 없는 준비된 문장과 포털을 없앤다 (PostgreSQL 과 같다)
        self.statements.pop("", None)
        self._drop_portal("")
        assert self.session is not None
        any_stmt = False
        try:
            for src in iter_statements(sql):
                any_stmt = True
                self._begin()
                try:
                    result = self.db.execute_one(src, self.session)
                    self._send_result(result, out, binary=None, max_rows=0, is_select=result.columns is not None)
                finally:
                    self._end()
        except DatabaseError as exc:
            if exc.severity == "FATAL":
                raise
            out.write(m.error_response(exc))  # 남은 문장은 실행하지 않는다
        if not any_stmt:
            out.write(m.empty_query_response())
        self._report_changes(out)
        out.write(m.ready_for_query())
        out.flush()

    def _send_result(
        self,
        result: QueryResult,
        out: Output,
        *,
        binary: list[bool] | None,
        max_rows: int,
        is_select: bool,
        portal: Portal | None = None,
        send_description: bool = True,
    ) -> None:
        """결과 하나를 보낸다: [RowDescription] DataRow* (CommandComplete | PortalSuspended)."""
        if result.columns is None:
            out.write(m.command_complete(result.command_tag))
            if portal is not None:
                portal.done = True
            return
        if send_description:
            out.write(m.row_description(result.columns))
        rows = portal.rows if portal is not None else iter(result.rows)
        assert rows is not None
        sent = 0
        encode = m.encode_row
        write = out.write
        for row in rows:
            write(encode(row, binary))
            sent += 1
            if sent == max_rows:
                break
        if max_rows > 0 and sent == max_rows:
            write(m.portal_suspended())  # 다음 Execute 에서 이어서 (PostgreSQL 처럼 미리 보기로 끝을 확인하지 않는다)
            return
        if portal is not None:
            portal.done = True
        # 스트리밍 SELECT 의 태그는 이번 Execute 에서 보낸 행 수 (PostgreSQL 과 같다)
        tag = f"SELECT {sent}" if is_select and _is_select_tag(result) else result.command_tag
        write(m.command_complete(tag))

    # ------------------------------------------------------------ 확장 쿼리
    def _parse(self, r: m.Reader, out: Output) -> None:
        name = r.cstring()
        sql = r.cstring()
        n = r.int16()
        if n < 0:
            raise ProtocolViolation("invalid number of parameter types")
        oids = [r.uint32() for _ in range(n)]
        r.end()
        if name and name in self.statements:
            raise DuplicatePreparedStatement(f'prepared statement "{name}" already exists')
        prepared = self.db.prepare(sql, oids)
        self.statements[name] = prepared  # 이름 없는 문장("")은 새 Parse 가 덮어쓴다
        out.write(m.parse_complete())

    def _get_statement(self, name: str) -> PreparedStatement:
        try:
            return self.statements[name]
        except KeyError:
            raise InvalidSqlStatementName(f'prepared statement "{name}" does not exist') from None

    def _get_portal(self, name: str) -> Portal:
        try:
            return self.portals[name]
        except KeyError:
            raise InvalidCursorName(f'portal "{name}" does not exist') from None

    def _drop_portal(self, name: str) -> None:
        p = self.portals.pop(name, None)
        if p is not None:
            p.close()

    def _bind(self, r: m.Reader, out: Output) -> None:
        portal_name = r.cstring()
        stmt_name = r.cstring()
        nfmt = r.int16()
        param_formats = [r.int16() for _ in range(max(nfmt, 0))]
        nparams = r.uint16()
        raw: list[bytes | None] = []
        for _ in range(nparams):
            size = r.int32()
            raw.append(None if size == -1 else r.bytes(size))
        nres = r.int16()
        result_formats = [r.int16() for _ in range(max(nres, 0))]
        r.end()

        prepared = self._get_statement(stmt_name)
        if nfmt not in (0, 1) and nfmt != nparams:
            raise ProtocolViolation(f"bind message has {nfmt} parameter formats but {nparams} parameters")
        need = len(prepared.param_types)
        if nparams != need:
            raise ProtocolViolation(
                f'bind message supplies {nparams} parameters, but prepared statement "{stmt_name}" requires {need}'
            )
        for f in (*param_formats, *result_formats):
            if f not in (0, 1):
                raise ProtocolViolation(f"unsupported format code: {f}")
        ncols = len(prepared.columns) if prepared.columns is not None else 0
        if nres not in (0, 1) and nres != ncols:
            raise ProtocolViolation(f"bind message has {nres} result formats but query has {ncols} columns")
        if portal_name and portal_name in self.portals:
            raise DuplicateCursor(f'cursor "{portal_name}" already exists')

        values = [
            decode_param(v, param_formats[i] if nfmt > 1 else (param_formats[0] if nfmt == 1 else 0), oid, i + 1)
            for i, (v, oid) in enumerate(zip(raw, prepared.param_types, strict=True))
        ]
        bound = bind_values(prepared.bound, values) if prepared.bound is not None else None
        formats = [result_formats[i] if nres > 1 else (result_formats[0] if nres == 1 else 0) for i in range(ncols)]
        binary = [f == 1 for f in formats] if any(formats) else None
        self._drop_portal(portal_name)  # 이름 없는 포털("")은 새 Bind 가 덮어쓴다
        self.portals[portal_name] = Portal(portal_name, prepared, bound, formats, binary=binary)
        out.write(m.bind_complete())

    def _describe(self, r: m.Reader, out: Output) -> None:
        what = r.byte()
        name = r.cstring()
        r.end()
        if what == 0x53:  # 'S' 준비된 문장: ParameterDescription + (RowDescription | NoData)
            prepared = self._get_statement(name)
            out.write(m.parameter_description(prepared.param_types))
            cols = prepared.columns
            out.write(m.row_description(cols) if cols is not None else m.no_data())
        elif what == 0x50:  # 'P' 포털: RowDescription (Bind 의 결과 형식 코드 반영) | NoData
            portal = self._get_portal(name)
            cols = portal.prepared.columns
            out.write(m.row_description(cols, portal.formats) if cols is not None else m.no_data())
        else:
            raise ProtocolViolation(f"invalid DESCRIBE message subtype {what}")

    def _execute(self, r: m.Reader, out: Output) -> None:
        name = r.cstring()
        max_rows = r.int32()
        r.end()
        portal = self._get_portal(name)
        if portal.bound is None:
            out.write(m.empty_query_response())
            return
        is_select = isinstance(portal.bound, BoundSelect)
        if portal.done:
            # 끝난 포털을 다시 실행: 보낼 행이 없다
            out.write(m.command_complete("SELECT 0" if is_select else _tag_only(portal)))
            return
        assert self.session is not None
        self.queries += 1
        self._begin()
        try:
            if portal.result is None:
                # 첫 Execute 에서 실행한다 (INSERT 는 여기서 완료, SELECT 는 첫 행을 당길 때 스냅샷을 잡는다)
                portal.result = self.db.execute_bound(portal.bound, self.session)
                if portal.result.columns is not None:
                    portal.rows = iter(portal.result.rows)
            self._send_result(
                portal.result,
                out,
                binary=portal.binary,
                max_rows=max(max_rows, 0),
                is_select=is_select,
                portal=portal,
                send_description=False,  # 확장 쿼리는 Describe 가 RowDescription 을 보낸다
            )
        except DatabaseError:
            portal.close()
            self.portals.pop(name, None)
            raise
        finally:
            self._end()

    def _close(self, r: m.Reader, out: Output) -> None:
        what = r.byte()
        name = r.cstring()
        r.end()
        if what == 0x53:  # 'S'
            self.statements.pop(name, None)  # 없는 이름을 닫는 것은 오류가 아니다 (PostgreSQL 과 같다)
        elif what == 0x50:  # 'P'
            self._drop_portal(name)
        else:
            raise ProtocolViolation(f"invalid CLOSE message subtype {what}")
        out.write(m.close_complete())

    def _sync(self, out: Output) -> None:
        self.skip_until_sync = False
        self._drop_portal("")  # 이름 없는 포털은 Sync 에서 닫는다 (트랜잭션 끝)
        self._report_changes(out)
        out.write(m.ready_for_query())
        out.flush()

    # ------------------------------------------------------------ 종료
    def close(self) -> None:
        """연결이 끝날 때: 열린 포털(스캔)을 모두 닫는다."""
        for p in self.portals.values():
            p.close()
        self.portals.clear()
        self.statements.clear()


def _is_select_tag(result: QueryResult) -> bool:
    return result._tag_prefix == "SELECT"  # noqa: SLF001 - 스트리밍 SELECT 인지 (태그를 이번 Execute 기준으로)


def _tag_only(portal: Portal) -> str:
    return portal.result.command_tag if portal.result is not None else "SELECT 0"
