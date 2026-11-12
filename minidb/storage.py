"""테이블 저장 계층: 레코드 인코딩/디코딩과 파일 I/O.

파일 포맷 v2 (모든 정수 little-endian, 자세한 내용은 docs/storage-format.md)::

    헤더 16B : magic b"MDB1" | format_version u16 = 2 | column_count u16 | reserved 8B
    레코드   : length u32 | body_crc u32 | header_crc u32 | body (flags u8 | id i64 | null_bitmap | fields...)
               length     = body 의 바이트 수 (레코드 전체 = 12 + length)
               body_crc   = zlib.crc32(body)
               header_crc = zlib.crc32(length 4B + body_crc 4B)

레코드는 헤더 뒤에 append 만 된다. 레코드의 파일 내 시작 위치(offset)가 곧 레코드 주소이며,
Phase 2 의 B+ 트리는 ``id -> offset`` 을 저장해 :meth:`TableFile.read_at` 으로 조회한다.
"""

from __future__ import annotations

import os
import struct
import warnings
import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO, Self

from .catalog import BIGINT, TableSchema
from .errors import (
    CharacterNotInRepertoire,
    DataCorrupted,
    DatatypeMismatch,
    InvalidParameterValue,
    NotNullViolation,
    NumericValueOutOfRange,
    ProgramLimitExceeded,
    SequenceGeneratorLimitExceeded,
)

__all__ = [
    "MAGIC",
    "FORMAT_VERSION",
    "HEADER_SIZE",
    "RECORD_HEADER_SIZE",
    "FLAG_DELETED",
    "BIGINT_MIN",
    "BIGINT_MAX",
    "RecordCodec",
    "RecordCheck",
    "TableFile",
    "inspect_records",
    "read_header",
]

# ---- 포맷 상수 (불변이므로 모듈 수준에 두어도 스레드 안전) ----
MAGIC: bytes = b"MDB1"
FORMAT_VERSION: int = 2
HEADER_SIZE: int = 16
RECORD_HEADER_SIZE: int = 12  # length + body_crc + header_crc
FLAG_DELETED: int = 0x01
_KNOWN_FLAGS: int = FLAG_DELETED

BIGINT_MIN: int = -(2**63)
BIGINT_MAX: int = 2**63 - 1

_HEADER = struct.Struct("<4sHH8x")  # magic, version, column_count, reserved(8)
_U32 = struct.Struct("<I")
_I64 = struct.Struct("<q")
_REC_HDR = struct.Struct("<III")  # length, body_crc, header_crc
_LEN_CRC = struct.Struct("<II")  # header_crc 계산 대상: length + body_crc
_FLAGS_ID = struct.Struct("<Bq")  # flags u8 + id i64 (본문 앞 9바이트)

assert _HEADER.size == HEADER_SIZE
assert _REC_HDR.size == RECORD_HEADER_SIZE

_SCAN_CHUNK = 1 << 20  # 순차 읽기 청크 크기 (1 MiB)
_WRITE_BUFFER = 1 << 18  # 쓰기 버퍼 크기 (256 KiB)
_PREAD_GUESS = 256  # read_at 에서 한 번에 읽어볼 바이트 수

_crc32 = zlib.crc32

# _iter_raw 가 내는 이벤트 종류
_EV_RECORD = 0  # header_crc 가 맞고 본문이 다 있는 레코드 (body_crc 결과 포함)
_EV_BAD_HEADER = 1  # header_crc 불일치: length 를 믿을 수 없어 더 진행하지 않음
_EV_BAD_LENGTH = 2  # header_crc 는 맞지만 length < 본문 최소 크기


def _now() -> datetime:
    """quarantine 파일 이름에 쓸 현재 시각 (로컬 시간). 테스트에서 교체할 수 있게 함수로 둔다."""
    return datetime.now()


def read_header(f: BinaryIO) -> tuple[bytes, int, int]:
    """파일 맨 앞 16바이트 헤더를 읽어 ``(magic, version, column_count)`` 를 반환한다.

    Raises:
        DataCorrupted: 헤더가 16바이트보다 짧을 때.
    """
    f.seek(0)
    raw = f.read(HEADER_SIZE)
    if len(raw) != HEADER_SIZE:
        raise DataCorrupted(f"file header is truncated ({len(raw)} of {HEADER_SIZE} bytes)")
    magic, version, column_count = _HEADER.unpack(raw)
    return magic, version, column_count


class RecordCodec:
    """스키마에 맞춰 레코드를 인코딩/디코딩한다. 상태가 없어(생성 후 불변) 스레드 간 공유 가능.

    Args:
        schema: 테이블 스키마.
        max_text_bytes: TEXT 값 하나의 최대 UTF-8 바이트 수.
    """

    def __init__(self, schema: TableSchema, max_text_bytes: int = 1_048_576) -> None:
        if max_text_bytes < 0:
            raise ValueError("max_text_bytes must be >= 0")
        self.schema: TableSchema = schema
        self.max_text_bytes: int = max_text_bytes
        cols = schema.data_columns
        self.ncols: int = len(cols)
        self.bitmap_len: int = (self.ncols + 7) // 8  # ceil(n / 8)
        self.min_body: int = 1 + 8 + self.bitmap_len  # flags + id + bitmap (모든 컬럼 NULL)
        self._is_bigint: tuple[bool, ...] = tuple(c.type == BIGINT for c in cols)
        self._nullable: tuple[bool, ...] = tuple(c.nullable for c in cols)
        self._col_names: tuple[str, ...] = tuple(c.name for c in cols)
        # 비트맵 마지막 바이트에서 실제 컬럼에 대응하지 않는 상위 비트 마스크 (손상 검출용)
        rem = self.ncols % 8
        self._bitmap_pad_mask: int = (0xFF << rem) & 0xFF if rem else 0

    def encode(self, values: Any, rid: int) -> bytes:
        """값 검증 후 레코드 하나(12바이트 레코드 헤더 포함)를 bytes 로 인코딩한다."""
        if not isinstance(values, (tuple, list)):
            raise InvalidParameterValue(f"row values must be a tuple, got {type(values).__name__}")
        if len(values) != self.ncols:
            raise InvalidParameterValue(
                f"table \"{self.schema.name}\" expects {self.ncols} values "
                f"({', '.join(self._col_names)}), got {len(values)}"
            )

        bitmap = bytearray(self.bitmap_len)
        parts: list[bytes] = []
        is_bigint = self._is_bigint
        for i, v in enumerate(values):
            if v is None:
                if not self._nullable[i]:
                    raise NotNullViolation(
                        f"null value in column \"{self._col_names[i]}\" of relation "
                        f"\"{self.schema.name}\" violates not-null constraint"
                    )
                # NULL 비트맵: i번째 컬럼 → (i // 8)번째 바이트의 (i % 8)번째 비트.
                # NULL 인 컬럼은 필드 영역에 아무것도 쓰지 않는다.
                bitmap[i >> 3] |= 1 << (i & 7)
                continue
            if is_bigint[i]:
                # bool 은 int 의 하위 클래스지만 SQL 에서는 별개 타입이므로 거부한다.
                if type(v) is bool or not isinstance(v, int):
                    raise DatatypeMismatch(
                        f"column \"{self._col_names[i]}\" is of type bigint "
                        f"but value is of type {type(v).__name__}"
                    )
                if not BIGINT_MIN <= v <= BIGINT_MAX:
                    raise NumericValueOutOfRange("bigint out of range")
                parts.append(_I64.pack(v))  # BIGINT: int64 8바이트
            else:
                if not isinstance(v, str):
                    raise DatatypeMismatch(
                        f"column \"{self._col_names[i]}\" is of type text "
                        f"but value is of type {type(v).__name__}"
                    )
                try:
                    data = v.encode("utf-8")
                except UnicodeEncodeError as exc:  # 짝 없는 서로게이트 등
                    raise CharacterNotInRepertoire(
                        f"value for column \"{self._col_names[i]}\" has no UTF-8 equivalent: {exc.reason}"
                    ) from None
                # 길이 제한은 글자 수가 아니라 UTF-8 바이트 수 기준.
                if len(data) > self.max_text_bytes:
                    raise ProgramLimitExceeded(
                        f"value for column \"{self._col_names[i]}\" is too long "
                        f"({len(data)} bytes, maximum is {self.max_text_bytes})"
                    )
                parts.append(_U32.pack(len(data)))  # TEXT: u32 바이트 길이 + UTF-8 바이트
                parts.append(data)

        # body = flags(u8) | id(i64) | bitmap | fields
        body = b"".join((_FLAGS_ID.pack(0, rid), bitmap, *parts))
        # 레코드 헤더: length | body_crc | header_crc.
        # header_crc 가 length 를 보호하므로, length 가 깨지면 "그 뒤를 해석하지 않고" 바로 손상으로 판정할 수 있다.
        len_crc = _LEN_CRC.pack(len(body), _crc32(body))
        return b"".join((len_crc, _U32.pack(_crc32(len_crc)), body))

    def decode(self, buf: bytes | memoryview, p: int, length: int, offset: int) -> tuple[int, tuple[Any, ...]]:
        """``buf[p:p+length]`` 의 레코드 본문을 ``(flags, row)`` 로 디코딩한다 (CRC 는 호출자가 검증).

        row 는 id 를 맨 앞에 포함한 전체 튜플. 구조가 맞지 않으면 DataCorrupted.
        """
        end = p + length
        try:
            flags, rid = _FLAGS_ID.unpack_from(buf, p)
            if flags & ~_KNOWN_FLAGS:
                raise DataCorrupted(f"unknown flags 0x{flags:02x} in record at offset {offset}")
            bp = p + 9
            bitmap = buf[bp : bp + self.bitmap_len]
            if self._bitmap_pad_mask and bitmap[-1] & self._bitmap_pad_mask:
                raise DataCorrupted(f"invalid null bitmap in record at offset {offset}")
            pos = bp + self.bitmap_len
            row: list[Any] = [rid]
            for i, is_bigint in enumerate(self._is_bigint):
                if bitmap[i >> 3] & (1 << (i & 7)):
                    row.append(None)
                elif is_bigint:
                    row.append(_I64.unpack_from(buf, pos)[0])
                    pos += 8
                else:
                    (n,) = _U32.unpack_from(buf, pos)
                    pos += 4
                    if pos + n > end:
                        raise DataCorrupted(f"text field overruns record at offset {offset}")
                    row.append(bytes(buf[pos : pos + n]).decode("utf-8"))
                    pos += n
        except (struct.error, UnicodeDecodeError, IndexError) as exc:
            raise DataCorrupted(f"malformed record at offset {offset}: {exc}") from None
        if pos != end:
            raise DataCorrupted(f"record at offset {offset} has {end - pos} unexpected trailing byte(s)")
        return flags, tuple(row)


class TableFile:
    """한 테이블의 데이터 파일을 다루는 객체.

    - :meth:`append` / :meth:`append_many` 로 레코드를 파일 끝에 추가하고 ``(id, offset)`` 을 돌려준다.
    - :meth:`read_at` 은 offset 으로 레코드 하나를, :meth:`scan` 은 전체 레코드를 순서대로 읽는다.
    - 쓰기는 한 번의 append 호출 안에서 버퍼링되고, 반환 직전에 OS 로 내보낸다(fsync 아님).
    - 모든 레코드는 header_crc / body_crc 로 보호된다. 열 때, read_at, scan 모두 검증한다.

    불변식 (Phase 3 의 RW 락 설계가 여기에 의존한다):

    (a) append 계열 메서드(:meth:`append`, :meth:`append_many`)가 반환된 시점에
        ``_end`` 이전의 모든 바이트는 OS 에 전달되어 있다. 따라서 같은 프로세스의 어떤
        파일 핸들(다른 TableFile 인스턴스 포함)도 그 레코드를 읽을 수 있다(read-your-writes).
    (b) :meth:`read_at` / :meth:`scan` / :meth:`records` 는 인스턴스의 어떤 속성도
        변경하지 않는다. 읽기끼리는 공유(read) 락만으로 병렬 실행할 수 있다.

    **스레드 안전하지 않다.** 이번 단계에서는 한 인스턴스를 한 스레드에서만 사용해야 한다.
    GIL 의 암묵적 원자성에도 기대지 않으며(3.14t 에서는 GIL 이 없다), Phase 3 에서
    메서드 단위 락을 추가할 예정이다. 공유 가변 상태는 모두 인스턴스 속성에 있고,
    락 지점은 ``# [LOCK:read]`` (공유 락) / ``# [LOCK:write]`` (배타 락) 주석으로 표시했다.

    Args:
        path: 데이터 파일 경로. 없거나 0바이트면 헤더를 써서 새로 만든다.
        schema: 테이블 스키마.
        max_text_bytes: TEXT 값 하나의 최대 UTF-8 바이트 수.
        readonly: True 면 파일을 절대 생성/수정하지 않는다(쓰기 메서드 사용 불가,
            잘린 꼬리는 quarantine/truncate 없이 논리적으로만 제외하고 경고). 덤프 도구용.
    """

    def __init__(
        self,
        path: str | Path,
        schema: TableSchema,
        *,
        max_text_bytes: int = 1_048_576,
        readonly: bool = False,
    ) -> None:
        self.path: Path = Path(path)
        self.schema: TableSchema = schema
        self.readonly: bool = readonly
        self._codec: RecordCodec = RecordCodec(schema, max_text_bytes)

        self._writer: BinaryIO | None = None
        self._reader: BinaryIO | None = None
        self._closed: bool = True
        #: 이번 open 에서 꼬리를 quarantine 한 파일 경로 (없으면 None)
        self.quarantine_path: Path | None = None

        self._open_or_create()
        # [LOCK:write] _end / _next_id 는 append 때마다 갱신되는 공유 상태.
        self._end, self._next_id = self._recover()

        self._reader = open(self.path, "rb", buffering=0)
        if not readonly:
            self._writer = open(self.path, "ab", buffering=_WRITE_BUFFER)
        self._closed = False

    @property
    def max_text_bytes(self) -> int:
        """TEXT 값 하나의 최대 UTF-8 바이트 수."""
        return self._codec.max_text_bytes

    # ------------------------------------------------------------------ open
    def _open_or_create(self) -> None:
        """파일이 없거나 비어 있으면 헤더를 쓰고, 있으면 헤더를 검증한다."""
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            size = 0
            if self.readonly:
                raise FileNotFoundError(f"table file not found: {self.path}") from None

        if size == 0:
            if self.readonly:
                raise DataCorrupted(f"table file is empty: {self.path}")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "wb") as f:
                f.write(_HEADER.pack(MAGIC, FORMAT_VERSION, len(self.schema.columns)))
                f.flush()
                os.fsync(f.fileno())
            return
        if size < HEADER_SIZE:
            raise DataCorrupted(
                f"file \"{self.path.name}\" is smaller than the {HEADER_SIZE}-byte header ({size} bytes)"
            )

        with open(self.path, "rb") as f:
            magic, version, column_count = read_header(f)
        if magic != MAGIC:
            raise DataCorrupted(f"invalid magic {magic!r} in \"{self.path.name}\" (expected {MAGIC!r})")
        if version != FORMAT_VERSION:
            raise DataCorrupted(
                f"unsupported storage format version {version} (expected {FORMAT_VERSION}) in \"{self.path.name}\""
            )
        if column_count != len(self.schema.columns):
            raise DataCorrupted(
                f"column count mismatch in \"{self.path.name}\": file has {column_count}, "
                f"schema \"{self.schema.name}\" has {len(self.schema.columns)}"
            )

    def _recover(self) -> tuple[int, int]:
        """전체 레코드를 한 번 훑어 (논리적 파일 끝, next_id) 를 복원한다.

        offset 위치의 레코드를 다음 순서로 판정한다 (docs/storage-format.md 의 표 참고):

        1. 남은 바이트 < 12                          → 잘린 꼬리
        2. header_crc 불일치                          → DataCorrupted (length 를 믿을 수 없음)
        3. length < 본문 최소 크기                     → DataCorrupted
        4. 남은 바이트 < 12 + length                  → 잘린 꼬리
        5. body_crc 불일치: 마지막 레코드면 잘린 꼬리, 아니면 DataCorrupted
        6. id 가 이전 레코드보다 크지 않음             → DataCorrupted

        필드 디코딩은 하지 않는다(두 CRC 와 id 만 확인). DataCorrupted 를 낼 때는 파일을 건드리지 않는다.
        """
        size = self.path.stat().st_size
        min_body = self._codec.min_body
        last_id = 0
        good_end = HEADER_SIZE  # 마지막으로 온전한 레코드의 끝 = 잘린 꼬리가 있으면 truncate 위치
        with open(self.path, "rb", buffering=0) as f:
            for kind, offset, buf, p, length, body_ok in _iter_raw(f, HEADER_SIZE, size, min_body):
                if kind == _EV_BAD_HEADER:  # 판정 2
                    raise DataCorrupted(
                        f"record header checksum mismatch at offset {offset} in \"{self.path.name}\""
                    )
                if kind == _EV_BAD_LENGTH:  # 판정 3
                    raise DataCorrupted(
                        f"record at offset {offset} has length {length} < minimum {min_body}"
                    )
                if not body_ok:  # 판정 5
                    if offset + RECORD_HEADER_SIZE + length == size:
                        # 마지막 레코드: 크래시 때 파일 크기만 늘고 내용이 0 등으로 채워진 경우 → 잘린 꼬리.
                        break
                    raise DataCorrupted(
                        f"record body checksum mismatch at offset {offset} in \"{self.path.name}\""
                    )
                (rid,) = _I64.unpack_from(buf, p + 1)
                if rid <= last_id:  # 판정 6
                    raise DataCorrupted(
                        f"record at offset {offset} has id {rid}, not greater than previous id {last_id}"
                    )
                last_id = rid
                good_end = offset + RECORD_HEADER_SIZE + length
        # 판정 1, 4 는 _iter_raw 가 조용히 멈추는 것으로 나타난다: good_end < size.

        if good_end != size:
            self._handle_torn_tail(good_end, size)
        return good_end, last_id + 1

    def _handle_torn_tail(self, truncate_pos: int, size: int) -> None:
        """잘린 꼬리 [truncate_pos, size) 처리: quarantine 파일로 보존한 뒤 truncate (readonly 면 경고만)."""
        lost = size - truncate_pos
        if self.readonly:
            warnings.warn(
                f"{self.path.name}: ignoring {lost} trailing byte(s) of an incomplete record "
                f"at offset {truncate_pos} (readonly, file not modified)",
                RuntimeWarning,
                stacklevel=4,
            )
            return
        # 비파괴 복구: 잘라낼 바이트를 먼저 별도 파일로 복사하고 fsync 한 뒤에야 truncate 한다.
        # (복사가 실패하면 예외가 그대로 올라가고 원본은 손대지 않은 상태로 남는다.)
        qpath = self._write_quarantine(truncate_pos, size)
        with open(self.path, "r+b") as f:
            f.truncate(truncate_pos)
            f.flush()
            os.fsync(f.fileno())
        self.quarantine_path = qpath
        warnings.warn(
            f"{self.path.name}: truncated {lost} trailing byte(s) of an incomplete record "
            f"at offset {truncate_pos}; removed bytes saved to {qpath}",
            RuntimeWarning,
            stacklevel=4,
        )

    def _write_quarantine(self, start: int, end: int) -> Path:
        """``[start, end)`` 를 ``<name>.corrupt-<YYYYmmddTHHMMSS>[-N]`` 파일로 복사·fsync 하고 경로를 반환한다."""
        base = f"{self.path.name}.corrupt-{_now():%Y%m%dT%H%M%S}"
        n = 0
        while True:
            qpath = self.path.with_name(base if n == 0 else f"{base}-{n}")
            try:
                out = open(qpath, "xb")  # 배타적 생성: 같은 이름이 있으면 덮어쓰지 않고 다음 접미사
            except FileExistsError:
                n += 1
                continue
            break
        with out, open(self.path, "rb") as src:
            src.seek(start)
            remaining = end - start
            while remaining > 0:
                chunk = src.read(min(_SCAN_CHUNK, remaining))
                if not chunk:
                    break
                out.write(chunk)
                remaining -= len(chunk)
            out.flush()
            os.fsync(out.fileno())
        _fsync_dir(self.path.parent)  # 새 파일의 디렉터리 엔트리도 디스크에 반영
        return qpath

    # --------------------------------------------------------------- helpers
    def _check_open(self) -> None:
        if self._closed:
            raise ValueError("I/O operation on closed TableFile")

    def _check_writable(self) -> None:
        self._check_open()
        if self._writer is None:
            raise ValueError("TableFile was opened readonly")

    # ------------------------------------------------------------ write API
    def append(self, values: tuple) -> tuple[int, int]:
        """레코드 하나를 추가하고 ``(id, offset)`` 을 반환한다.

        Args:
            values: data_columns 순서의 값 튜플 (id 제외).

        Raises:
            InvalidParameterValue, DatatypeMismatch, NumericValueOutOfRange, ProgramLimitExceeded,
            NotNullViolation, CharacterNotInRepertoire: 값 검증 실패 (아무것도 쓰지 않음).
            SequenceGeneratorLimitExceeded: id 가 BIGINT 최댓값을 넘을 때.
        """
        return self.append_many((values,))[0]

    def append_many(self, rows: Iterable[tuple]) -> list[tuple[int, int]]:
        """여러 레코드를 원자적으로 추가하고 각 ``(id, offset)`` 목록을 반환한다.

        모든 행을 먼저 검증/인코딩한 뒤, 전부 통과하면 한 번의 write 로 쓰고 한 번 OS 로 내보낸다.
        하나라도 실패하면 아무것도 쓰지 않고 next_id 도 그대로다.
        (프로세스 크래시 시의 원자성은 보장하지 않는다. 반쯤 쓰인 꼬리는 다음 open 때 잘린다.)
        """
        self._check_writable()
        assert self._writer is not None
        # [LOCK:write] 여기서부터 끝까지가 임계 구역: _next_id 읽기 → id 할당 → write → _end/_next_id 갱신.
        # GIL 이 있어도 이 일련의 과정은 원자적이지 않다.
        encode = self._codec.encode
        next_id = self._next_id
        offset = self._end
        encoded: list[bytes] = []
        result: list[tuple[int, int]] = []
        for values in rows:
            if next_id > BIGINT_MAX:
                raise SequenceGeneratorLimitExceeded(
                    f"nextval: reached maximum value of sequence for table \"{self.schema.name}\" ({BIGINT_MAX})"
                )
            rec = encode(values, next_id)  # 실패하면 여기서 예외 → 아무 상태도 안 바뀜
            encoded.append(rec)
            result.append((next_id, offset))
            offset += len(rec)
            next_id += 1
        if not encoded:
            return result

        data = encoded[0] if len(encoded) == 1 else b"".join(encoded)
        try:
            self._writer.write(data)
            # 불변식 (a): 반환 전에 배치 전체를 한 번에 OS 로 내보낸다 (fsync 는 하지 않음).
            # 덕분에 읽기 메서드는 쓰기 버퍼를 건드릴 필요가 없다 → 불변식 (b).
            self._writer.flush()
        except BaseException:
            # 디스크 부족 등으로 일부만 쓰였을 수 있다: 상태를 갱신하지 않고 핸들을 닫아
            # 이후 사용을 막는다. 다시 열면 꼬리 복구가 반쯤 쓰인 레코드를 잘라낸다.
            self._abort()
            raise
        self._end = offset
        self._next_id = next_id
        return result

    # ------------------------------------------------------------- read API
    def read_at(self, offset: int) -> tuple | None:
        """``offset`` 위치의 레코드를 읽어 id 를 포함한 행 튜플을 반환한다 (deleted 면 None).

        Raises:
            ValueError: offset 이 정수가 아니거나 데이터 영역 [16, 파일 끝) 밖일 때.
            DataCorrupted: offset 이 레코드 경계가 아니거나(header_crc 불일치 등),
                레코드가 손상되었을 때(body_crc 불일치).
        """
        self._check_open()
        if type(offset) is not int:
            raise ValueError(f"offset must be an int, got {type(offset).__name__}")
        # [LOCK:read] _end/_next_id 스냅샷. 아래 pread 는 파일 위치를 공유하지 않아 병렬 가능.
        end = self._end
        next_id = self._next_id
        if not HEADER_SIZE <= offset < end:
            raise ValueError(f"offset {offset} out of range [{HEADER_SIZE}, {end})")

        assert self._reader is not None
        fd = self._reader.fileno()
        buf = _pread(fd, min(_PREAD_GUESS, end - offset), offset)
        if len(buf) < RECORD_HEADER_SIZE:
            raise DataCorrupted(f"offset {offset} is not a valid record boundary")
        length, body_crc, header_crc = _REC_HDR.unpack_from(buf, 0)
        # 레코드 경계가 아닌 offset 에서 읽은 12바이트가 우연히 header_crc 까지 맞을 확률은 약 2^-32.
        if _crc32(buf[:8]) != header_crc:
            raise DataCorrupted(f"offset {offset} is not a valid record boundary (header checksum mismatch)")
        total = RECORD_HEADER_SIZE + length
        if length < self._codec.min_body or offset + total > end:
            raise DataCorrupted(f"offset {offset} is not a valid record boundary (bad length {length})")
        if len(buf) < total:
            buf += _pread(fd, total - len(buf), offset + len(buf))
            if len(buf) < total:
                raise DataCorrupted(f"record at offset {offset} is truncated")
        body = memoryview(buf)[RECORD_HEADER_SIZE:total]
        if _crc32(body) != body_crc:
            raise DataCorrupted(f"record body checksum mismatch at offset {offset}")
        flags, row = self._codec.decode(buf, RECORD_HEADER_SIZE, length, offset)
        if not 1 <= row[0] < next_id:
            raise DataCorrupted(f"record at offset {offset} has out-of-range id {row[0]}")
        return None if flags & FLAG_DELETED else row

    def records(self) -> Iterator[tuple[int, int, tuple]]:
        """deleted 를 포함한 모든 레코드를 ``(offset, flags, row)`` 로 순서대로 내보낸다.

        제너레이터 생성 시점의 파일 끝까지만 읽는다. 독립된 읽기 핸들을 쓰므로
        순회 도중 read_at/append 를 호출해도 순회 위치가 깨지지 않는다.
        레코드마다 두 CRC 를 검증하고 불일치면 DataCorrupted.
        """
        self._check_open()
        # [LOCK:read] _end 스냅샷. 이후 순회는 전용 fd 만 쓰고 인스턴스 상태를 건드리지 않는다.
        end = self._end
        return self._records_gen(end)

    def _records_gen(self, end: int) -> Iterator[tuple[int, int, tuple]]:
        decode = self._codec.decode
        with open(self.path, "rb", buffering=0) as f:
            last = HEADER_SIZE
            for kind, offset, buf, p, length, body_ok in _iter_raw(f, HEADER_SIZE, end, self._codec.min_body):
                if kind == _EV_BAD_HEADER:
                    raise DataCorrupted(f"record header checksum mismatch at offset {offset}")
                if kind == _EV_BAD_LENGTH:
                    raise DataCorrupted(f"record at offset {offset} has invalid length {length}")
                if not body_ok:
                    raise DataCorrupted(f"record body checksum mismatch at offset {offset}")
                flags, row = decode(buf, p, length, offset)
                last = offset + RECORD_HEADER_SIZE + length
                yield offset, flags, row
            if last != end:
                raise DataCorrupted(f"incomplete record at offset {last}")

    def scan(self) -> Iterator[tuple[int, tuple]]:
        """deleted 가 아닌 레코드를 ``(offset, row)`` 로 순서대로 내보내는 제너레이터.

        파일 전체를 메모리에 올리지 않고 1 MiB 청크 단위로 읽으며 하나씩 검증·디코딩한다.
        닫힌 TableFile 에서 호출하면 (첫 next() 가 아니라) 호출 즉시 ValueError.
        """
        return ((offset, row) for offset, flags, row in self.records() if not flags & FLAG_DELETED)

    # ------------------------------------------------------------ properties
    @property
    def next_id(self) -> int:
        """다음 append 에 할당될 id."""
        return self._next_id

    @property
    def size(self) -> int:
        """논리적 파일 크기(바이트). 불변식 (a) 에 따라 append 반환 후에는 OS 상 크기와 같다."""
        return self._end

    # ------------------------------------------------------------- lifecycle
    def flush(self, *, durable: bool = False) -> None:
        """버퍼의 쓰기를 OS 로 내보낸다. ``durable`` 이면 ``os.fsync`` 로 디스크까지 보장."""
        self._check_open()
        if self._writer is None:
            return
        # [LOCK:write] 쓰기 핸들 사용. append 가 이미 flush 하므로 보통은 fsync 용도.
        self._writer.flush()
        if durable:
            os.fsync(self._writer.fileno())

    def close(self) -> None:
        """버퍼를 flush 하고 파일 핸들을 닫는다. 여러 번 호출해도 안전."""
        if self._closed:
            return
        try:
            if self._writer is not None:
                # [LOCK:write] 쓰기 핸들 사용 + 닫힘 상태 전이.
                self._writer.flush()
        finally:
            self._abort()

    def _abort(self) -> None:
        """핸들을 (flush 시도 없이) 닫고 인스턴스를 닫힌 상태로 만든다."""
        self._closed = True
        for h in (self._writer, self._reader):
            if h is not None:
                try:
                    h.close()
                except OSError:
                    pass
        self._writer = None
        self._reader = None

    @property
    def closed(self) -> bool:
        """닫혔는지 여부."""
        return self._closed

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        state = "closed" if self._closed else f"next_id={self._next_id}, size={self._end}"
        return f"<TableFile {self.schema.name!r} {str(self.path)!r} {state}>"


# ------------------------------------------------------------ 진단용 검사
@dataclass(frozen=True, slots=True)
class RecordCheck:
    """:func:`inspect_records` 가 레코드 위치마다 내는 검사 결과.

    Attributes:
        offset: 레코드 시작 위치.
        length: 레코드 헤더의 length (헤더가 12바이트 미만이면 None).
        header_ok: header_crc 일치 여부.
        body_ok: body_crc 일치 여부 (본문을 검사하지 못했으면 None).
        flags: 디코딩 성공 시 flags.
        row: 디코딩 성공 시 행 튜플.
        error: 문제가 있으면 설명 (정상이면 None).
    """

    offset: int
    length: int | None
    header_ok: bool
    body_ok: bool | None
    flags: int | None
    row: tuple | None
    error: str | None


def inspect_records(path: str | Path, schema: TableSchema) -> Iterator[RecordCheck]:
    """파일을 수정하지 않고 레코드마다 CRC/구조를 검사해 :class:`RecordCheck` 를 낸다 (덤프 도구용).

    TableFile 과 달리 손상을 만나도 예외를 내지 않는다. body_crc 만 깨진 레코드는 건너뛰고
    계속 진행하지만, header_crc 가 깨지면 length 를 믿을 수 없으므로 거기서 멈춘다.
    파일 헤더(magic/version/column_count)는 검사하지 않는다 — 호출자가 :func:`read_header` 로 확인한다.
    """
    codec = RecordCodec(schema)
    path = Path(path)
    size = path.stat().st_size
    last = HEADER_SIZE
    with open(path, "rb", buffering=0) as f:
        for kind, offset, buf, p, length, body_ok in _iter_raw(f, HEADER_SIZE, size, codec.min_body):
            if kind == _EV_BAD_HEADER:
                yield RecordCheck(offset, length, False, None, None, None, "header checksum mismatch (stopped)")
                return
            if kind == _EV_BAD_LENGTH:
                yield RecordCheck(offset, length, True, None, None, None, f"length {length} < minimum (stopped)")
                return
            last = offset + RECORD_HEADER_SIZE + length
            if not body_ok:
                yield RecordCheck(offset, length, True, False, None, None, "body checksum mismatch")
                continue
            try:
                flags, row = codec.decode(buf, p, length, offset)
            except DataCorrupted as exc:
                yield RecordCheck(offset, length, True, True, None, None, exc.message)
                continue
            yield RecordCheck(offset, length, True, True, flags, row, None)
    if last < size:
        yield RecordCheck(last, None, False, None, None, None, f"incomplete tail: {size - last} byte(s)")


# ---------------------------------------------------------------- 내부 함수
def _fsync_dir(directory: Path) -> None:
    """디렉터리 엔트리 변경(새 파일 생성)을 디스크에 반영한다. 지원하지 않는 플랫폼에서는 무시."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:  # pragma: no cover - Windows 등
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover
        pass
    finally:
        os.close(fd)


def _pread(fd: int, n: int, offset: int) -> bytes:
    """``offset`` 에서 최대 ``n`` 바이트를 읽는다 (짧게 읽히면 반복)."""
    out = b""
    while len(out) < n:
        if hasattr(os, "pread"):
            chunk = os.pread(fd, n - len(out), offset + len(out))
        else:  # pragma: no cover - Windows 등 pread 가 없는 플랫폼
            os.lseek(fd, offset + len(out), os.SEEK_SET)
            chunk = os.read(fd, n - len(out))
        if not chunk:
            break
        out += chunk
    return out


def _iter_raw(
    f: BinaryIO, start: int, end: int, min_body: int
) -> Iterator[tuple[int, int, bytes, int, int, bool]]:
    """``[start, end)`` 를 청크 단위로 읽으며 ``(kind, offset, buf, body_pos, length, body_ok)`` 를 낸다.

    - ``_EV_RECORD``: header_crc 가 맞고 본문이 다 있는 레코드. ``buf[body_pos:body_pos+length]`` 가 본문,
      ``body_ok`` 는 body_crc 일치 여부.
    - ``_EV_BAD_HEADER`` / ``_EV_BAD_LENGTH``: 내보낸 뒤 즉시 멈춘다 (그 뒤는 해석할 수 없음).

    구간 끝의 불완전한 레코드(12바이트 미만이거나 본문이 모자람)는 내보내지 않고 조용히 멈춘다 —
    호출자가 마지막 레코드 끝과 ``end`` 를 비교해 잘린 꼬리를 판단한다.
    ``min_body`` (본문 최소 크기) 검사는 header_crc 확인 직후, 본문을 더 읽기 전에 한다(판정 3 → 4 순서).
    """
    f.seek(start)
    buf = b""
    mv = memoryview(buf)
    bpos = 0  # buf 안에서 다음 레코드 시작 위치
    file_pos = start  # buf[bpos] 에 해당하는 파일 offset
    read_pos = start  # 다음에 파일에서 읽을 위치
    unpack_hdr = _REC_HDR.unpack_from
    crc = _crc32
    min_len = max(min_body, 9)  # 어떤 경우에도 flags + id 는 있어야 한다
    while True:
        avail = len(buf) - bpos
        need = RECORD_HEADER_SIZE
        if avail >= RECORD_HEADER_SIZE:
            length, body_crc, header_crc = unpack_hdr(buf, bpos)
            # header_crc 를 먼저 확인한다. 이게 틀리면 length 자체를 믿을 수 없으므로
            # length 만큼 더 읽으려 하지 않고(= 잘린 꼬리로 오판하지 않고) 바로 손상으로 보고한다.
            if crc(mv[bpos : bpos + 8]) != header_crc:
                yield _EV_BAD_HEADER, file_pos, buf, bpos + RECORD_HEADER_SIZE, length, False
                return
            if length < min_len:
                yield _EV_BAD_LENGTH, file_pos, buf, bpos + RECORD_HEADER_SIZE, length, False
                return
            need = RECORD_HEADER_SIZE + length
            if avail >= need:
                body_pos = bpos + RECORD_HEADER_SIZE
                body_ok = crc(mv[body_pos : bpos + need]) == body_crc
                yield _EV_RECORD, file_pos, buf, body_pos, length, body_ok
                bpos += need
                file_pos += need
                continue
        left = end - read_pos
        if left <= 0:
            return
        chunk = f.read(min(max(_SCAN_CHUNK, need - avail), left))
        if not chunk:
            return
        read_pos += len(chunk)
        # 처리 안 된 꼬리 + 새 청크를 이어 붙인다 (꼬리는 보통 레코드 1개 미만으로 작다).
        buf = buf[bpos:] + chunk
        mv = memoryview(buf)
        bpos = 0
