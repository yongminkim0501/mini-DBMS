"""테이블 핸들: 데이터 파일(TableFile)과 id 인덱스(B+ 트리)를 한 묶음으로 다룬다.

- **파일이 진실의 원천**이고 인덱스는 파생 데이터다. 인덱스는 디스크에 저장하지 않고, 테이블을 열 때마다
  파일 검증 스캔과 **같은 한 번의 스캔**으로 벌크 로딩한다 (``TableFile(on_entry=builder.add)``).
- INSERT 는 :meth:`TableHandle.append_many` 로 파일에 먼저 쓰고, 성공하면 ``(id, offset)`` 을 순서대로 인덱스에 넣는다.
- 인덱스 갱신 중 예상치 못한 예외가 나면 인덱스를 무효화한다. 다음에 인덱스가 필요할 때 파일에서 다시 구성하고
  경고(RuntimeWarning)를 낸다. 파일에는 이미 온전히 쓰였으므로 INSERT 자체는 성공으로 둔다.

**스레드 안전** (Phase 3). 테이블마다 :class:`~minidb.concurrency.RWLock` 하나 (``handle.lock``):

- 쓰기(INSERT): **배타 락 안에서** 파일 append(반환 시 OS 로 flush 완료) → 인덱스 insert → 락 해제.
  락이 풀리는 순간이 커밋 시점이다. 파일 쓰기와 인덱스 갱신을 같은 배타 구간에 넣으므로, 다른 스레드는
  "파일엔 있는데 인덱스엔 없는" 상태를 보지 못한다.
- 읽기(SELECT): **공유 락을 잠깐만** 잡고 스냅샷(파일 끝 offset, 필요하면 인덱스 조회 결과)을 기록한 뒤 바로 푼다.
  스냅샷 이전의 바이트는 append-only 라 다시는 바뀌지 않으므로, 실제 레코드 읽기는 락 없이 한다
  (:meth:`snapshot_end`, :meth:`point_lookup`, :meth:`range_start`). 문장 단위 스냅샷 읽기 (docs/server.md).
- 인덱스 무효화 후 재구성은 배타 락 안에서, 파일 핸들을 바꾸지 않고 열린 파일을 다시 훑어서 한다
  (락 밖에서 읽는 중인 스캔이 쓰는 파일 객체를 닫으면 안 되므로).
"""

from __future__ import annotations

import warnings
from collections.abc import Iterable
from pathlib import Path

from .catalog import TableSchema
from .concurrency import RWLock
from .errors import UniqueViolation
from .index import DEFAULT_ORDER, BPlusTree, DuplicateKeyError
from .storage import TableFile

__all__ = ["TableHandle", "index_name_for", "NAME_MAX_BYTES"]

NAME_MAX_BYTES = 63  # PostgreSQL NAMEDATALEN - 1


def index_name_for(table: str) -> str:
    """기본 키 인덱스 이름 ``<table>_pkey``.

    PostgreSQL(ChooseRelationName)처럼 전체가 63바이트를 넘으면 테이블 이름 쪽을 UTF-8 문자 경계에서 잘라 맞춘다.
    """
    suffix = "_pkey"
    budget = NAME_MAX_BYTES - len(suffix)
    raw = table.encode("utf-8")
    if len(raw) > budget:
        table = raw[:budget].decode("utf-8", errors="ignore")  # 잘린 멀티바이트 문자는 버린다
    return table + suffix


def _open_with_index(
    path: Path, schema: TableSchema, max_text_bytes: int, order: int
) -> tuple[TableFile, BPlusTree]:
    """파일을 열면서(검증 스캔 한 번) 같은 스캔으로 인덱스를 벌크 로딩한다."""
    builder = BPlusTree.bulk_builder(order)
    tf = TableFile(path, schema, max_text_bytes=max_text_bytes, on_entry=builder.add)
    try:
        index = builder.finish()
    except BaseException:
        tf.close()
        raise
    return tf, index


class TableHandle:
    """한 테이블의 TableFile + id 인덱스 + RW 락.

    Attributes:
        file: 데이터 파일.
        schema: 테이블 스키마.
        index_name: ``<table>_pkey``.
        lock: 테이블 RW 락.
        lock_timeout: 락 대기 제한 (초, None 이면 무한). 넘으면 LockNotAvailable (55P03).
        rebuilds: 인덱스를 무효화 후 다시 구성한 횟수 (테스트/진단용).
    """

    __slots__ = (
        "file",
        "schema",
        "index_name",
        "path",
        "order",
        "max_text_bytes",
        "lock",
        "lock_timeout",
        "_index",
        "rebuilds",
    )

    def __init__(
        self,
        file: TableFile,
        index: BPlusTree,
        *,
        order: int = DEFAULT_ORDER,
        lock_timeout: float | None = None,
    ) -> None:
        self.file = file
        self.schema = file.schema
        self.path = file.path
        self.index_name = index_name_for(file.schema.name)
        self.order = order
        self.max_text_bytes = file.max_text_bytes
        self.lock = RWLock(file.schema.name)
        self.lock_timeout = lock_timeout
        # self.lock 아래에서만 바꾼다 (읽기는 공유 락, 교체는 배타 락)
        self._index: BPlusTree | None = index
        self.rebuilds = 0

    @classmethod
    def open(
        cls,
        path: str | Path,
        schema: TableSchema,
        *,
        max_text_bytes: int = 1_048_576,
        order: int = DEFAULT_ORDER,
        lock_timeout: float | None = None,
    ) -> TableHandle:
        """파일을 열고(없으면 생성) 인덱스를 구성한다. 파일 스캔은 한 번."""
        tf, index = _open_with_index(Path(path), schema, max_text_bytes, order)
        return cls(tf, index, order=order, lock_timeout=lock_timeout)

    # ------------------------------------------------------------ 인덱스
    @property
    def index_valid(self) -> bool:
        with self.lock.read_locked(self.lock_timeout):
            return self._index is not None

    @property
    def index(self) -> BPlusTree:
        """id 인덱스 (진단·테스트용). 무효화된 상태면 배타 락 아래에서 파일로 재구성하고 경고한다.

        반환된 트리를 락 없이 순회하면 동시 INSERT 와 경쟁한다. 엔진은 이 속성 대신 :meth:`point_lookup` /
        :meth:`range_start` 를 쓴다.
        """
        with self.lock.read_locked(self.lock_timeout):
            index = self._index
        if index is None:
            with self.lock.write_locked(self.lock_timeout):
                index = self._ensure_index_locked()
        return index

    def _ensure_index_locked(self) -> BPlusTree:
        """(배타 락을 가진 상태에서) 인덱스가 없으면 다시 만든다."""
        if self._index is None:
            self._rebuild_locked()
        assert self._index is not None
        return self._index

    def _rebuild_locked(self) -> None:
        # 열린 파일을 처음부터 다시 훑는다 (CRC 검증 + 디코딩). 파일 객체는 그대로 두므로
        # 락 밖에서 진행 중인 스냅샷 스캔(같은 TableFile 의 read_at/scan_from)이 영향받지 않는다.
        builder = BPlusTree.bulk_builder(self.order)
        for offset, row in self.file.scan():
            builder.add(row[0], offset)
        index = builder.finish()
        self._index = index
        self.rebuilds += 1
        warnings.warn(
            f'index "{self.index_name}" was invalidated by an earlier error and has been rebuilt '
            f"from the table file ({len(index)} entries)",
            RuntimeWarning,
            stacklevel=4,
        )

    def invalidate_index(self) -> None:
        """인덱스를 버린다. 다음 접근 때 파일에서 다시 구성된다."""
        with self.lock.write_locked(self.lock_timeout):
            self._index = None

    def _read_index(self) -> BPlusTree:
        """공유 락을 가진 상태에서 쓸 인덱스. 무효화돼 있으면 None 대신 재구성을 요청한다(호출자가 재시도)."""
        index = self._index
        if index is None:
            raise _NeedRebuild
        return index

    def _with_index(self, fn):  # type: ignore[no-untyped-def]
        """공유 락 안에서 ``fn(index, end)`` 를 부른다. 인덱스가 무효면 배타 락으로 재구성한 뒤 다시 시도."""
        while True:
            try:
                with self.lock.read_locked(self.lock_timeout):
                    # [LOCK:read] 스냅샷: 인덱스 조회 결과와 파일 끝을 같은 공유 구간에서 기록한다.
                    return fn(self._read_index(), self.file.size)
            except _NeedRebuild:
                with self.lock.write_locked(self.lock_timeout):
                    self._ensure_index_locked()

    # ------------------------------------------------------------ 스냅샷 읽기
    def snapshot_end(self) -> int:
        """공유 락 안에서 파일 끝 offset 을 기록한다. 이 offset 이전의 레코드는 모두 커밋된 것이다."""
        with self.lock.read_locked(self.lock_timeout):
            return self.file.size  # [LOCK:read]

    def point_lookup(self, key: int) -> tuple[int | None, int]:
        """``(key 의 offset 또는 None, 스냅샷 끝)``. 트리 검색과 스냅샷 기록이 같은 공유 구간에서 일어난다."""
        return self._with_index(lambda index, end: (index.search(key), end))

    def range_start(
        self, lo: int | None, hi: int | None, lo_inclusive: bool, hi_inclusive: bool
    ) -> tuple[int | None, int]:
        """``(하한 이상·상한 이하인 첫 키의 offset 또는 None, 스냅샷 끝)``."""

        def first(index: BPlusTree, end: int) -> tuple[int | None, int]:
            it = index.range(lo, hi, lo_inclusive, hi_inclusive)
            try:
                item = next(it, None)
            finally:
                it.close()  # type: ignore[attr-defined]
            return (None if item is None else item[1]), end

        return self._with_index(first)

    # ------------------------------------------------------------ 쓰기
    def append_many(self, rows: Iterable[tuple]) -> list[tuple[int, int]]:
        """배타 락 안에서 파일에 행들을 원자적으로 추가하고, 인덱스에 ``(id, offset)`` 을 순서대로 넣는다.

        락이 풀리는 시점이 커밋 시점이다: 이후에 스냅샷을 잡는 SELECT 는 이 행들을 본다.

        Raises:
            저장 계층의 검증 오류: 아무것도 쓰지 않음 (인덱스도 그대로).
            UniqueViolation: 인덱스에 이미 있는 id (자동 id 라 정상 경로에서는 일어나지 않는 방어 코드).
                인덱스는 무효화되어 다음 접근 때 파일에서 다시 구성된다.
            LockNotAvailable: lock_timeout 안에 배타 락을 얻지 못함 (아무것도 쓰지 않음).
        """
        rows = list(rows)  # 락 밖에서 입력을 다 받아 둔다 (제너레이터가 오래 걸려도 락을 붙잡지 않게)
        with self.lock.write_locked(self.lock_timeout):
            # [LOCK:write] 파일 append 와 인덱스 갱신이 한 임계 구역.
            index = self._ensure_index_locked()  # 무효화된 상태면 쓰기 전에 먼저 재구성
            placed = self.file.append_many(rows)
            try:
                for rid, offset in placed:
                    index.insert(rid, offset)
            except DuplicateKeyError as exc:
                self._index = None
                raise UniqueViolation(
                    f'duplicate key value violates unique constraint "{self.index_name}"',
                    detail=f"Key (id)=({exc.key}) already exists.",
                ) from None
            except Exception:
                # 파일에는 온전히 쓰였다 (진실의 원천). 인덱스만 버리고 INSERT 는 성공으로 둔다.
                self._index = None
            except BaseException:
                # KeyboardInterrupt 등: 인덱스가 반쯤 갱신됐을 수 있으므로 버린 뒤 그대로 올린다.
                self._index = None
                raise
            return placed

    # ------------------------------------------------------------ 수명
    def flush(self, *, durable: bool = False) -> None:
        with self.lock.write_locked(self.lock_timeout):
            self.file.flush(durable=durable)

    def close(self) -> None:
        with self.lock.write_locked(self.lock_timeout):
            self.file.close()

    @property
    def closed(self) -> bool:
        return self.file.closed

    def __repr__(self) -> str:
        state = f"{len(self._index)} keys" if self._index is not None else "invalid"
        return f"TableHandle({self.schema.name!r}, index={self.index_name!r} {state})"


class _NeedRebuild(Exception):
    """공유 락 안에서 인덱스가 무효임을 발견했다 (배타 락으로 재구성 후 재시도)."""
