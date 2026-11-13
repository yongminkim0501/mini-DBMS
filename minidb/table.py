"""테이블 핸들: 데이터 파일(TableFile)과 id 인덱스(B+ 트리)를 한 묶음으로 다룬다.

- **파일이 진실의 원천**이고 인덱스는 파생 데이터다. 인덱스는 디스크에 저장하지 않고, 테이블을 열 때마다
  파일 검증 스캔과 **같은 한 번의 스캔**으로 벌크 로딩한다 (``TableFile(on_entry=builder.add)``).
- INSERT 는 :meth:`TableHandle.append_many` 로 파일에 먼저 쓰고, 성공하면 ``(id, offset)`` 을 순서대로 인덱스에 넣는다.
- 인덱스 갱신 중 예상치 못한 예외가 나면 인덱스를 무효화한다. 다음에 인덱스가 필요할 때 파일에서 다시 구성하고
  경고(RuntimeWarning)를 낸다. 파일에는 이미 온전히 쓰였으므로 INSERT 자체는 성공으로 둔다.

**스레드 안전하지 않다.** Phase 3 락 계획 (``# [LOCK:*]``):
테이블마다 RW 락 하나. SELECT(파일 읽기 + 인덱스 search/range)는 공유 락, INSERT(append + 인덱스 insert)와
인덱스 재구성은 배타 락. 파일 쓰기와 인덱스 갱신을 **같은** 배타 구간에 넣어야 "파일엔 있는데 인덱스엔 없는" 순간을
다른 스레드가 보지 않는다.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterable
from pathlib import Path

from .catalog import TableSchema
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
    """한 테이블의 TableFile + id 인덱스.

    Attributes:
        file: 데이터 파일.
        schema: 테이블 스키마.
        index_name: ``<table>_pkey``.
        rebuilds: 인덱스를 무효화 후 다시 구성한 횟수 (테스트/진단용).
    """

    __slots__ = ("file", "schema", "index_name", "path", "order", "max_text_bytes", "_index", "rebuilds")

    def __init__(self, file: TableFile, index: BPlusTree, *, order: int = DEFAULT_ORDER) -> None:
        self.file = file
        self.schema = file.schema
        self.path = file.path
        self.index_name = index_name_for(file.schema.name)
        self.order = order
        self.max_text_bytes = file.max_text_bytes
        # [LOCK:write] _index 교체(무효화/재구성)는 배타 락.
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
    ) -> TableHandle:
        """파일을 열고(없으면 생성) 인덱스를 구성한다. 파일 스캔은 한 번."""
        tf, index = _open_with_index(Path(path), schema, max_text_bytes, order)
        return cls(tf, index, order=order)

    # ------------------------------------------------------------ 인덱스
    @property
    def index_valid(self) -> bool:
        return self._index is not None

    @property
    def index(self) -> BPlusTree:
        """id 인덱스. 무효화된 상태면 파일을 다시 열어(검증 스캔 한 번) 재구성하고 경고한다."""
        index = self._index
        if index is None:
            index = self._rebuild()
        return index

    def _rebuild(self) -> BPlusTree:
        # [LOCK:write] 파일 핸들과 인덱스를 함께 교체한다.
        self.file.close()
        self.file, index = _open_with_index(self.path, self.schema, self.max_text_bytes, self.order)
        self._index = index
        self.rebuilds += 1
        warnings.warn(
            f'index "{self.index_name}" was invalidated by an earlier error and has been rebuilt '
            f"from the table file ({len(index)} entries)",
            RuntimeWarning,
            stacklevel=3,
        )
        return index

    def invalidate_index(self) -> None:
        """인덱스를 버린다. 다음 :attr:`index` 접근 때 파일에서 다시 구성된다."""
        # [LOCK:write]
        self._index = None

    # ------------------------------------------------------------ 쓰기
    def append_many(self, rows: Iterable[tuple]) -> list[tuple[int, int]]:
        """파일에 행들을 원자적으로 추가하고, 성공하면 인덱스에 ``(id, offset)`` 을 순서대로 넣는다.

        Raises:
            저장 계층의 검증 오류: 아무것도 쓰지 않음 (인덱스도 그대로).
            UniqueViolation: 인덱스에 이미 있는 id (자동 id 라 정상 경로에서는 일어나지 않는 방어 코드).
                인덱스는 무효화되어 다음 접근 때 파일에서 다시 구성된다.
        """
        # [LOCK:write] 파일 append 와 인덱스 갱신이 한 임계 구역 (Phase 3).
        index = self.index  # 무효화된 상태면 쓰기 전에 먼저 재구성
        placed = self.file.append_many(rows)
        try:
            for rid, offset in placed:
                index.insert(rid, offset)
        except DuplicateKeyError as exc:
            self.invalidate_index()
            raise UniqueViolation(
                f'duplicate key value violates unique constraint "{self.index_name}"',
                detail=f"Key (id)=({exc.key}) already exists.",
            ) from None
        except Exception:
            # 파일에는 온전히 쓰였다 (진실의 원천). 인덱스만 버리고 INSERT 는 성공으로 둔다.
            self.invalidate_index()
        except BaseException:
            # KeyboardInterrupt 등: 인덱스가 반쯤 갱신됐을 수 있으므로 버린 뒤 그대로 올린다.
            self.invalidate_index()
            raise
        return placed

    # ------------------------------------------------------------ 수명
    def flush(self, *, durable: bool = False) -> None:
        self.file.flush(durable=durable)

    def close(self) -> None:
        self.file.close()

    @property
    def closed(self) -> bool:
        return self.file.closed

    def __repr__(self) -> str:
        state = f"{len(self._index)} keys" if self._index is not None else "invalid"
        return f"TableHandle({self.schema.name!r}, index={self.index_name!r} {state})"
