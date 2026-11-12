"""테이블 저장 계층: 레코드 인코딩/디코딩과 파일 I/O.

파일 포맷 (모든 정수 little-endian, 자세한 내용은 docs/storage-format.md)::

    헤더 16B : magic b"MDB1" | format_version u16 | column_count u16 | reserved 8B
    레코드   : length u32 | flags u8 | id i64 | null_bitmap | fields...
               (length = length 필드 뒤 본문의 바이트 수)

레코드는 헤더 뒤에 append 만 된다. 레코드의 파일 내 시작 위치(offset)가 곧 레코드 주소이며,
Phase 2 의 B+ 트리는 ``id -> offset`` 을 저장해 :meth:`TableFile.read_at` 으로 조회한다.
"""

from __future__ import annotations

import os
import struct
import warnings
from collections.abc import Iterable, Iterator
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
    "FLAG_DELETED",
    "BIGINT_MIN",
    "BIGINT_MAX",
    "TableFile",
    "read_header",
]

# ---- 포맷 상수 (불변이므로 모듈 수준에 두어도 스레드 안전) ----
MAGIC: bytes = b"MDB1"
FORMAT_VERSION: int = 1
HEADER_SIZE: int = 16
FLAG_DELETED: int = 0x01
_KNOWN_FLAGS: int = FLAG_DELETED

BIGINT_MIN: int = -(2**63)
BIGINT_MAX: int = 2**63 - 1

_HEADER = struct.Struct("<4sHH8x")  # magic, version, column_count, reserved(8)
_U32 = struct.Struct("<I")
_I64 = struct.Struct("<q")
_FLAGS_ID = struct.Struct("<Bq")  # flags u8 + id i64 (본문 앞 9바이트)
_LEN_FLAGS_ID = struct.Struct("<IBq")  # length + flags + id (레코드 앞 13바이트)

assert _HEADER.size == HEADER_SIZE

_SCAN_CHUNK = 1 << 20  # 순차 읽기 청크 크기 (1 MiB)
_WRITE_BUFFER = 1 << 18  # 쓰기 버퍼 크기 (256 KiB)
_PREAD_GUESS = 256  # read_at 에서 한 번에 읽어볼 바이트 수


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


class TableFile:
    """한 테이블의 데이터 파일을 다루는 객체.

    - :meth:`append` / :meth:`append_many` 로 레코드를 파일 끝에 추가하고 ``(id, offset)`` 을 돌려준다.
    - :meth:`read_at` 은 offset 으로 레코드 하나를, :meth:`scan` 은 전체 레코드를 순서대로 읽는다.
    - 쓰기는 버퍼링되지만 읽기 전에 내부적으로 flush 하므로 read-your-writes 가 보장된다.

    **스레드 안전하지 않다.** 이번 단계에서는 한 인스턴스를 한 스레드에서만 사용해야 한다.
    GIL 의 암묵적 원자성에도 기대지 않으며(3.14t 에서는 GIL 이 없다), Phase 3 에서
    메서드 단위 락을 추가할 예정이다. 공유 가변 상태는 모두 인스턴스 속성에 있고,
    락으로 보호해야 할 지점은 ``# [LOCK]`` 주석으로 표시했다.

    Args:
        path: 데이터 파일 경로. 없거나 0바이트면 헤더를 써서 새로 만든다.
        schema: 테이블 스키마.
        max_text_bytes: TEXT 값 하나의 최대 UTF-8 바이트 수.
        readonly: True 면 파일을 생성/수정하지 않는다(쓰기 메서드 사용 불가,
            손상된 꼬리는 truncate 하지 않고 경고 후 무시). 덤프 도구용.
    """

    def __init__(
        self,
        path: str | Path,
        schema: TableSchema,
        *,
        max_text_bytes: int = 1_048_576,
        readonly: bool = False,
    ) -> None:
        if max_text_bytes < 0:
            raise ValueError("max_text_bytes must be >= 0")
        self.path: Path = Path(path)
        self.schema: TableSchema = schema
        self.max_text_bytes: int = max_text_bytes
        self.readonly: bool = readonly

        cols = schema.data_columns
        self._ncols: int = len(cols)
        self._bitmap_len: int = (self._ncols + 7) // 8  # ceil(n / 8)
        self._min_body: int = 1 + 8 + self._bitmap_len  # flags + id + bitmap
        self._is_bigint: tuple[bool, ...] = tuple(c.type == BIGINT for c in cols)
        self._nullable: tuple[bool, ...] = tuple(c.nullable for c in cols)
        self._col_names: tuple[str, ...] = tuple(c.name for c in cols)
        # 비트맵 마지막 바이트에서 실제 컬럼에 대응하지 않는 상위 비트 마스크 (손상 검출용)
        rem = self._ncols % 8
        self._bitmap_pad_mask: int = (0xFF << rem) & 0xFF if rem else 0

        self._writer: BinaryIO | None = None
        self._reader: BinaryIO | None = None
        self._dirty: bool = False
        self._closed: bool = True

        self._open_or_create()
        # [LOCK] _end / _next_id 는 append 때마다 갱신되는 공유 상태.
        self._end, self._next_id = self._recover()

        self._reader = open(self.path, "rb", buffering=0)
        if not readonly:
            self._writer = open(self.path, "ab", buffering=_WRITE_BUFFER)
        self._closed = False

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
            raise DataCorrupted(f"file \"{self.path.name}\" is smaller than the {HEADER_SIZE}-byte header ({size} bytes)")

        with open(self.path, "rb") as f:
            magic, version, column_count = read_header(f)
        if magic != MAGIC:
            raise DataCorrupted(f"invalid magic {magic!r} in \"{self.path.name}\" (expected {MAGIC!r})")
        if version != FORMAT_VERSION:
            raise DataCorrupted(f"unsupported format version {version} in \"{self.path.name}\"")
        if column_count != len(self.schema.columns):
            raise DataCorrupted(
                f"column count mismatch in \"{self.path.name}\": file has {column_count}, "
                f"schema \"{self.schema.name}\" has {len(self.schema.columns)}"
            )

    def _recover(self) -> tuple[int, int]:
        """전체 레코드를 한 번 훑어 (논리적 파일 끝, next_id) 를 복원한다.

        손상된 꼬리 복구 규칙:
        - 남은 바이트가 4 미만이거나(length 필드조차 없음),
          length 가 가리키는 만큼 본문이 없으면 → 마지막으로 온전한 레코드 끝으로 truncate + 경고.
          (append 도중 프로세스가 죽어 마지막 레코드가 반쯤 쓰인 경우를 가정)
        - length 가 본문 최소 크기보다 작거나, id 가 단조 증가하지 않으면 → 중간 손상이므로 DataCorrupted.
        """
        size = self.path.stat().st_size
        last_id = 0
        good_end = HEADER_SIZE  # 마지막으로 온전한 레코드의 끝 위치
        with open(self.path, "rb", buffering=0) as f:
            for offset, buf, p, length in _iter_raw(f, HEADER_SIZE, size):
                if length < self._min_body:
                    raise DataCorrupted(
                        f"record at offset {offset} has length {length} < minimum {self._min_body}"
                    )
                _flags, rid = _FLAGS_ID.unpack_from(buf, p)
                if rid <= last_id:
                    raise DataCorrupted(
                        f"record at offset {offset} has id {rid}, not greater than previous id {last_id}"
                    )
                last_id = rid
                good_end = offset + 4 + length

        if good_end != size:
            # 꼬리 손상: good_end 이후는 온전한 레코드가 아니다.
            lost = size - good_end
            if self.readonly:
                warnings.warn(
                    f"{self.path.name}: ignoring {lost} trailing byte(s) of an incomplete record "
                    f"at offset {good_end} (readonly, file not modified)",
                    RuntimeWarning,
                    stacklevel=3,
                )
            else:
                with open(self.path, "r+b") as f:
                    f.truncate(good_end)
                    f.flush()
                    os.fsync(f.fileno())
                warnings.warn(
                    f"{self.path.name}: truncated {lost} trailing byte(s) of an incomplete record "
                    f"at offset {good_end}",
                    RuntimeWarning,
                    stacklevel=3,
                )
        return good_end, last_id + 1

    # --------------------------------------------------------------- helpers
    def _check_open(self) -> None:
        if self._closed:
            raise ValueError("I/O operation on closed TableFile")

    def _check_writable(self) -> None:
        self._check_open()
        if self._writer is None:
            raise ValueError("TableFile was opened readonly")

    def _sync_reads(self) -> None:
        """read-your-writes: 버퍼에 남은 쓰기를 OS 로 내보내 읽기 핸들이 볼 수 있게 한다."""
        # [LOCK] 읽기가 쓰기 버퍼를 건드리는 유일한 지점: append 와 상호 배제 필요.
        if self._dirty and self._writer is not None:
            self._writer.flush()
            self._dirty = False

    # -------------------------------------------------------------- encoding
    def _encode(self, values: Any, rid: int) -> bytes:
        """값 검증 후 레코드 하나(length 필드 포함)를 bytes 로 인코딩한다."""
        if not isinstance(values, (tuple, list)):
            raise InvalidParameterValue(
                f"row values must be a tuple, got {type(values).__name__}"
            )
        if len(values) != self._ncols:
            raise InvalidParameterValue(
                f"table \"{self.schema.name}\" expects {self._ncols} values "
                f"({', '.join(self._col_names)}), got {len(values)}"
            )

        bitmap = bytearray(self._bitmap_len)
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
                        f"value for column \"{self._col_names[i]}\" is not valid UTF-8: {exc.reason}"
                    ) from None
                # 길이 제한은 글자 수가 아니라 UTF-8 바이트 수 기준.
                if len(data) > self.max_text_bytes:
                    raise ProgramLimitExceeded(
                        f"value for column \"{self._col_names[i]}\" is too long "
                        f"({len(data)} bytes, maximum is {self.max_text_bytes})"
                    )
                parts.append(_U32.pack(len(data)))  # TEXT: u32 바이트 길이 + UTF-8 바이트
                parts.append(data)

        fields = b"".join(parts)
        body_len = self._min_body + len(fields)
        # length(u32) | flags(u8) | id(i64) | bitmap | fields
        return b"".join((_LEN_FLAGS_ID.pack(body_len, 0, rid), bitmap, fields))

    def _decode(self, buf: bytes | memoryview, p: int, length: int, offset: int) -> tuple[int, tuple[Any, ...]]:
        """``buf[p:p+length]`` 의 레코드 본문을 ``(flags, row)`` 로 디코딩한다.

        row 는 id 를 맨 앞에 포함한 전체 튜플. 구조가 맞지 않으면 DataCorrupted.
        """
        end = p + length
        try:
            flags, rid = _FLAGS_ID.unpack_from(buf, p)
            if flags & ~_KNOWN_FLAGS:
                raise DataCorrupted(f"unknown flags 0x{flags:02x} in record at offset {offset}")
            bp = p + 9
            bitmap = buf[bp : bp + self._bitmap_len]
            if self._bitmap_pad_mask and bitmap[-1] & self._bitmap_pad_mask:
                raise DataCorrupted(f"invalid null bitmap in record at offset {offset}")
            pos = bp + self._bitmap_len
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
            raise DataCorrupted(
                f"record at offset {offset} has {end - pos} unexpected trailing byte(s)"
            )
        return flags, tuple(row)

    # ------------------------------------------------------------ write API
    def append(self, values: tuple) -> tuple[int, int]:
        """레코드 하나를 추가하고 ``(id, offset)`` 을 반환한다.

        Args:
            values: data_columns 순서의 값 튜플 (id 제외).

        Raises:
            InvalidParameterValue, DatatypeMismatch, NumericValueOutOfRange,
            ProgramLimitExceeded, NotNullViolation: 값 검증 실패 (아무것도 쓰지 않음).
        """
        return self.append_many((values,))[0]

    def append_many(self, rows: Iterable[tuple]) -> list[tuple[int, int]]:
        """여러 레코드를 원자적으로 추가하고 각 ``(id, offset)`` 목록을 반환한다.

        모든 행을 먼저 검증/인코딩한 뒤, 전부 통과하면 한 번의 write 로 쓴다.
        하나라도 실패하면 아무것도 쓰지 않고 next_id 도 그대로다.
        (프로세스 크래시 시의 원자성은 보장하지 않는다. 반쯤 쓰인 꼬리는 다음 open 때 잘린다.)
        """
        self._check_writable()
        assert self._writer is not None
        # [LOCK] 여기서부터 끝까지가 임계 구역: _next_id 읽기 → id 할당 → write → _end/_next_id 갱신.
        # GIL 이 있어도 이 일련의 과정은 원자적이지 않다.
        next_id = self._next_id
        offset = self._end
        encoded: list[bytes] = []
        result: list[tuple[int, int]] = []
        for values in rows:
            if next_id > BIGINT_MAX:
                raise SequenceGeneratorLimitExceeded(
                    f"nextval: reached maximum value of sequence for table \"{self.schema.name}\" ({BIGINT_MAX})"
                )
            rec = self._encode(values, next_id)  # 실패하면 여기서 예외 → 아무 상태도 안 바뀜
            encoded.append(rec)
            result.append((next_id, offset))
            offset += len(rec)
            next_id += 1
        if not encoded:
            return result

        data = encoded[0] if len(encoded) == 1 else b"".join(encoded)
        try:
            self._writer.write(data)
        except BaseException:
            # 디스크 부족 등으로 일부만 쓰였을 수 있다: 상태를 갱신하지 않고 핸들을 닫아
            # 이후 사용을 막는다. 다시 열면 꼬리 복구가 반쯤 쓰인 레코드를 잘라낸다.
            self._abort()
            raise
        self._dirty = True
        self._end = offset
        self._next_id = next_id
        return result

    # ------------------------------------------------------------- read API
    def read_at(self, offset: int) -> tuple | None:
        """``offset`` 위치의 레코드를 읽어 id 를 포함한 행 튜플을 반환한다 (deleted 면 None).

        Raises:
            ValueError: offset 이 정수가 아니거나 데이터 영역 [16, 파일 끝) 밖일 때.
            DataCorrupted: offset 이 레코드 경계가 아니어서 구조가 맞지 않을 때.
        """
        self._check_open()
        if type(offset) is not int:
            raise ValueError(f"offset must be an int, got {type(offset).__name__}")
        # [LOCK] _end/_next_id 스냅샷 (읽기 락). 아래 pread 자체는 위치 공유가 없어 병렬 가능.
        end = self._end
        if not HEADER_SIZE <= offset < end:
            raise ValueError(f"offset {offset} out of range [{HEADER_SIZE}, {end})")
        self._sync_reads()

        assert self._reader is not None
        fd = self._reader.fileno()
        # pread 는 파일 위치를 공유하지 않으므로 Phase 3 에서 읽기끼리는 락 없이도 안전하다.
        buf = _pread(fd, min(_PREAD_GUESS, end - offset), offset)
        if len(buf) < 4:
            raise DataCorrupted(f"no record header at offset {offset}")
        (length,) = _U32.unpack_from(buf, 0)
        if length < self._min_body or offset + 4 + length > end:
            raise DataCorrupted(f"offset {offset} is not a valid record boundary")
        if len(buf) < 4 + length:
            buf += _pread(fd, 4 + length - len(buf), offset + len(buf))
            if len(buf) < 4 + length:
                raise DataCorrupted(f"record at offset {offset} is truncated")
        flags, row = self._decode(buf, 4, length, offset)
        if row[0] >= self._next_id or row[0] < 1:
            raise DataCorrupted(f"offset {offset} is not a valid record boundary (id {row[0]})")
        return None if flags & FLAG_DELETED else row

    def records(self) -> Iterator[tuple[int, int, tuple]]:
        """deleted 를 포함한 모든 레코드를 ``(offset, flags, row)`` 로 순서대로 내보낸다.

        제너레이터 시작 시점의 파일 끝까지만 읽는다. 독립된 읽기 핸들을 쓰므로
        순회 도중 read_at/append 를 호출해도 순회 위치가 깨지지 않는다.
        """
        self._check_open()
        self._sync_reads()
        # [LOCK] _end 스냅샷: append 와 일관된 값을 얻으려면 읽기 락 아래에서 읽는다.
        end = self._end
        return self._records_gen(end)

    def _records_gen(self, end: int) -> Iterator[tuple[int, int, tuple]]:
        decode = self._decode
        with open(self.path, "rb", buffering=0) as f:
            last = HEADER_SIZE
            for offset, buf, p, length in _iter_raw(f, HEADER_SIZE, end):
                flags, row = decode(buf, p, length, offset)
                last = offset + 4 + length
                yield offset, flags, row
            if last != end:
                raise DataCorrupted(f"incomplete record at offset {last}")

    def scan(self) -> Iterator[tuple[int, tuple]]:
        """deleted 가 아닌 레코드를 ``(offset, row)`` 로 순서대로 내보내는 제너레이터.

        파일 전체를 메모리에 올리지 않고 1 MiB 청크 단위로 읽으며 하나씩 디코딩한다.
        닫힌 TableFile 에서 호출하면 (첫 next() 가 아니라) 호출 즉시 ValueError.
        """
        return (
            (offset, row)
            for offset, flags, row in self.records()
            if not flags & FLAG_DELETED
        )

    # ------------------------------------------------------------ properties
    @property
    def next_id(self) -> int:
        """다음 append 에 할당될 id."""
        return self._next_id

    @property
    def size(self) -> int:
        """버퍼에 있는 쓰기를 포함한 논리적 파일 크기(바이트)."""
        return self._end

    # ------------------------------------------------------------- lifecycle
    def flush(self, *, durable: bool = False) -> None:
        """버퍼의 쓰기를 OS 로 내보낸다. ``durable`` 이면 ``os.fsync`` 로 디스크까지 보장."""
        self._check_open()
        if self._writer is None:
            return
        # [LOCK] flush: 쓰기 핸들/_dirty 접근 (쓰기 락).
        self._writer.flush()
        self._dirty = False
        if durable:
            os.fsync(self._writer.fileno())

    def close(self) -> None:
        """버퍼를 flush 하고 파일 핸들을 닫는다. 여러 번 호출해도 안전."""
        if self._closed:
            return
        try:
            if self._writer is not None:
                # [LOCK] close: 쓰기 핸들 사용 + 닫힘 상태 전이 (쓰기 락).
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


# ---------------------------------------------------------------- 내부 함수
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


def _iter_raw(f: BinaryIO, start: int, end: int) -> Iterator[tuple[int, bytes, int, int]]:
    """``[start, end)`` 구간을 청크 단위로 읽어 온전한 레코드마다 ``(offset, buf, body_pos, length)`` 를 낸다.

    ``buf[body_pos : body_pos + length]`` 가 레코드 본문이다. 구간 끝에 남은 불완전한
    레코드(4바이트 미만이거나 length 만큼 데이터가 없음)는 내보내지 않고 조용히 멈춘다 —
    호출자가 마지막 레코드 끝과 ``end`` 를 비교해 꼬리 손상을 판단한다.
    """
    f.seek(start)
    buf = b""
    bpos = 0  # buf 안에서 다음 레코드 시작 위치
    file_pos = start  # buf[bpos] 에 해당하는 파일 offset
    read_pos = start  # 다음에 파일에서 읽을 위치
    unpack_len = _U32.unpack_from
    while True:
        avail = len(buf) - bpos
        need = 4
        if avail >= 4:
            (length,) = unpack_len(buf, bpos)
            need = 4 + length
            if avail >= need:
                yield file_pos, buf, bpos + 4, length
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
        bpos = 0
