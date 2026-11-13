"""쿼리 결과: :class:`ColumnInfo` 와 :class:`QueryResult`.

Phase 3 에서 PostgreSQL wire protocol 로 그대로 내보낼 수 있게 설계했다:
``ColumnInfo`` → RowDescription, ``QueryResult.rows`` → DataRow 들, ``command_tag`` → CommandComplete.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from .catalog import BIGINT, TEXT

__all__ = ["BIGINT_OID", "TEXT_OID", "ColumnInfo", "QueryResult"]

#: PostgreSQL 타입 OID (pg_type.oid). RowDescription 의 type OID 필드에 쓴다.
BIGINT_OID = 20  # int8
TEXT_OID = 25  # text

_TYPE_INFO: dict[str, tuple[str, int]] = {BIGINT: ("bigint", BIGINT_OID), TEXT: ("text", TEXT_OID)}


@dataclass(frozen=True, slots=True)
class ColumnInfo:
    """결과 컬럼 하나의 이름과 타입.

    Attributes:
        name: 컬럼 이름.
        type_name: PostgreSQL 타입 이름 (``bigint`` / ``text``).
        type_oid: PostgreSQL 타입 OID (bigint=20, text=25).
        table_oid: 테이블 컬럼이면 테이블의 oid, 계산 컬럼(EXPLAIN, SHOW)이면 0 (RowDescription 용).
        column_number: 테이블 컬럼 번호 (1부터), 계산 컬럼이면 0 (RowDescription 용).
    """

    name: str
    type_name: str
    type_oid: int
    table_oid: int = field(default=0, compare=False)
    column_number: int = field(default=0, compare=False)

    @classmethod
    def for_type(cls, name: str, sql_type: str, *, table_oid: int = 0, column_number: int = 0) -> ColumnInfo:
        """카탈로그 타입(``BIGINT``/``TEXT``)으로 ColumnInfo 를 만든다."""
        type_name, oid = _TYPE_INFO[sql_type]
        return cls(name, type_name, oid, table_oid, column_number)

    @property
    def is_numeric(self) -> bool:
        """숫자 타입인가 (psql 은 숫자 컬럼을 오른쪽 정렬한다)."""
        return self.type_oid == BIGINT_OID


class QueryResult:
    """문장 하나의 실행 결과.

    - ``columns``: 결과 행이 있는 문장(SELECT, RETURNING, EXPLAIN)이면 컬럼 정보, 없으면 None.
    - ``rows``: 결과 행. SELECT 는 **이터레이터**로 흘려보내므로 한 번만 순회할 수 있다(대량 결과에서 메모리 절약).
    - ``command_tag``: PostgreSQL CommandComplete 태그 (``INSERT 0 3``, ``SELECT 10``, ``EXPLAIN``).

    스트리밍 결과의 ``SELECT n`` 태그는 행을 다 읽어야 n 을 알 수 있다. 그래서 ``command_tag`` 를 읽으면
    남은 행을 모두 소비(버림)한 뒤 태그를 돌려준다 (wire protocol 에서도 CommandComplete 는 DataRow 뒤에 온다).
    """

    __slots__ = ("columns", "_rows", "_tag", "_tag_prefix", "_count")

    def __init__(
        self,
        columns: list[ColumnInfo] | None,
        rows: Iterable[tuple[Any, ...]] = (),
        command_tag: str | None = None,
        *,
        tag_prefix: str | None = None,
    ) -> None:
        """
        Args:
            columns: 컬럼 정보 (행이 없는 명령이면 None).
            rows: 결과 행 (리스트 또는 이터레이터).
            command_tag: 이미 아는 태그. None 이면 ``tag_prefix`` 와 실제로 내보낸 행 수로 만든다.
            tag_prefix: ``command_tag`` 가 None 일 때 쓸 접두사 (예: ``"SELECT"``).
        """
        if command_tag is None and tag_prefix is None:
            raise ValueError("command_tag or tag_prefix is required")
        self.columns = columns
        self._tag = command_tag
        self._tag_prefix = tag_prefix
        self._count = 0
        self._rows: Iterator[tuple[Any, ...]] | list[tuple[Any, ...]]
        if command_tag is None:
            self._rows = self._counting(iter(rows))
        else:
            self._rows = rows if isinstance(rows, list) else list(rows)

    def _counting(self, it: Iterator[tuple[Any, ...]]) -> Iterator[tuple[Any, ...]]:
        for row in it:
            self._count += 1
            yield row
        self._tag = f"{self._tag_prefix} {self._count}"

    @property
    def rows(self) -> Iterator[tuple[Any, ...]] | list[tuple[Any, ...]]:
        """결과 행 (스트리밍이면 한 번만 순회 가능)."""
        return self._rows

    def __iter__(self) -> Iterator[tuple[Any, ...]]:
        return iter(self._rows)

    def fetchall(self) -> list[tuple[Any, ...]]:
        """남은 행을 모두 리스트로 읽는다."""
        return list(self._rows)

    @property
    def command_tag(self) -> str:
        """CommandComplete 태그. 스트리밍 결과면 남은 행을 소비한 뒤 반환한다."""
        if self._tag is None:
            for _ in self._rows:
                pass
        assert self._tag is not None
        return self._tag

    def __repr__(self) -> str:
        cols = None if self.columns is None else [c.name for c in self.columns]
        return f"<QueryResult columns={cols} tag={self._tag!r}>"
