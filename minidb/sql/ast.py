"""SQL 구문 트리(AST) 노드. 모두 frozen dataclass (불변 → 스레드 간 공유 안전).

모든 노드는 ``position`` (원문 기준 1부터 시작하는 문자 위치)을 가진다. 바인더가 오류 위치를 표시할 때 쓴다.
``position`` 과 ``*_positions`` 필드는 ``compare=False`` 라서 ``==`` 비교에서 제외된다.
그래서 ``parse(to_sql(ast)) == ast`` 왕복 테스트가 공백/위치 차이와 관계없이 성립한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .tokens import RESERVED_KEYWORDS

__all__ = [
    "QualifiedName",
    "Literal",
    "Default",
    "Star",
    "Condition",
    "InsertStmt",
    "SelectStmt",
    "Statement",
    "COMPARISON_OPS",
    "quote_ident",
    "quote_literal",
]

#: 정규화된 비교 연산자
COMPARISON_OPS: frozenset[str] = frozenset({"=", "<>", "<", "<=", ">", ">="})

_PLAIN_IDENT_RE = re.compile(r"[a-z_][a-z0-9_$]*\Z")
# 파서가 위치에 따라 "지원하지 않는 기능"으로 해석하는 단어들 (예: WHERE 뒤의 or/not).
# 이런 이름의 식별자는 to_sql 에서 항상 인용해야 왕복이 깨지지 않는다.
CONTEXTUAL_WORDS: frozenset[str] = frozenset(
    {
        "or", "not", "is", "in", "like", "ilike", "between", "order", "group", "having", "offset",
        "join", "inner", "left", "right", "full", "cross", "natural", "union", "intersect", "except",
        "fetch", "for", "as", "distinct", "on", "using", "window",
    }
)  # fmt: skip
_QUOTE_ALWAYS: frozenset[str] = frozenset(k.lower() for k in RESERVED_KEYWORDS) | CONTEXTUAL_WORDS


def quote_ident(name: str) -> str:
    """식별자를 SQL 로 쓴다. 비인용으로 같은 이름이 되지 않는 경우에만 큰따옴표로 감싼다."""
    if _PLAIN_IDENT_RE.match(name) and name not in _QUOTE_ALWAYS:
        return name
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: int | str | None) -> str:
    """리터럴 값을 SQL 로 쓴다 (문자열은 ``''`` 이스케이프, None 은 NULL)."""
    if value is None:
        return "NULL"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return str(value)


@dataclass(frozen=True, slots=True)
class QualifiedName:
    """``[schema.]name`` 형태의 테이블 이름."""

    schema: str | None
    name: str
    position: int = field(default=0, compare=False, kw_only=True)

    def to_sql(self) -> str:
        """SQL 표현."""
        base = quote_ident(self.name)
        return base if self.schema is None else f"{quote_ident(self.schema)}.{base}"


@dataclass(frozen=True, slots=True)
class Literal:
    """리터럴. ``value`` 가 None 이면 SQL NULL. 정수 범위 검사는 바인더가 한다."""

    value: int | str | None
    position: int = field(default=0, compare=False, kw_only=True)

    def to_sql(self) -> str:
        """SQL 표현."""
        return quote_literal(self.value)


@dataclass(frozen=True, slots=True)
class Default:
    """VALUES 안의 ``DEFAULT`` 키워드."""

    position: int = field(default=0, compare=False, kw_only=True)

    def to_sql(self) -> str:
        """SQL 표현."""
        return "DEFAULT"


@dataclass(frozen=True, slots=True)
class Star:
    """``SELECT *`` 또는 ``RETURNING *`` 의 ``*``."""

    position: int = field(default=0, compare=False, kw_only=True)

    def to_sql(self) -> str:
        """SQL 표현."""
        return "*"


@dataclass(frozen=True, slots=True)
class Condition:
    """``column op literal`` 비교 하나. 항상 컬럼이 왼쪽이 되도록 정규화되어 있다.

    ``position`` 은 컬럼 식별자의 위치 (바인더의 "column does not exist" 오류용).
    리터럴의 위치는 ``value.position``.
    """

    column: str
    op: str
    value: Literal
    position: int = field(default=0, compare=False, kw_only=True)

    def __post_init__(self) -> None:
        if self.op not in COMPARISON_OPS:
            raise ValueError(f"invalid comparison operator {self.op!r}")

    def to_sql(self) -> str:
        """SQL 표현."""
        return f"{quote_ident(self.column)} {self.op} {self.value.to_sql()}"


def _ident_list(names: tuple[str, ...]) -> str:
    return ", ".join(quote_ident(n) for n in names)


@dataclass(frozen=True, slots=True)
class InsertStmt:
    """``INSERT INTO table [(columns)] VALUES rows [RETURNING ...]``.

    Attributes:
        table: 대상 테이블.
        columns: 컬럼 목록. 생략하면 None.
        rows: 행 목록. 모든 행의 값 개수는 같다 (파서가 보장).
        returning: ``Star`` / 컬럼 이름 튜플 / None (RETURNING 없음).
        column_positions: ``columns`` 각 이름의 위치 (비교 제외).
        returning_positions: ``returning`` 이 컬럼 목록일 때 각 이름의 위치 (비교 제외).
    """

    table: QualifiedName
    columns: tuple[str, ...] | None
    rows: tuple[tuple[Literal | Default, ...], ...]
    returning: tuple[str, ...] | Star | None
    position: int = field(default=0, compare=False, kw_only=True)
    column_positions: tuple[int, ...] = field(default=(), compare=False, kw_only=True)
    returning_positions: tuple[int, ...] = field(default=(), compare=False, kw_only=True)

    def to_sql(self) -> str:
        """정규화된 SQL 문자열."""
        parts = [f"INSERT INTO {self.table.to_sql()}"]
        if self.columns is not None:
            parts.append(f"({_ident_list(self.columns)})")
        rows = ", ".join("(" + ", ".join(v.to_sql() for v in row) + ")" for row in self.rows)
        parts.append(f"VALUES {rows}")
        if isinstance(self.returning, Star):
            parts.append("RETURNING *")
        elif self.returning is not None:
            parts.append(f"RETURNING {_ident_list(self.returning)}")
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class SelectStmt:
    """``SELECT columns FROM table [WHERE cond AND ...] [LIMIT n]``.

    Attributes:
        columns: ``Star`` 또는 컬럼 이름 튜플.
        table: 대상 테이블.
        where: AND 로 연결된 조건들 (없으면 빈 튜플).
        limit: 행 수 제한. 없거나 ``LIMIT ALL`` 이면 None.
        column_positions: ``columns`` 가 이름 튜플일 때 각 이름의 위치 (비교 제외).
    """

    columns: tuple[str, ...] | Star
    table: QualifiedName
    where: tuple[Condition, ...]
    limit: int | None
    position: int = field(default=0, compare=False, kw_only=True)
    column_positions: tuple[int, ...] = field(default=(), compare=False, kw_only=True)

    def to_sql(self) -> str:
        """정규화된 SQL 문자열."""
        cols = "*" if isinstance(self.columns, Star) else _ident_list(self.columns)
        sql = f"SELECT {cols} FROM {self.table.to_sql()}"
        if self.where:
            sql += " WHERE " + " AND ".join(c.to_sql() for c in self.where)
        if self.limit is not None:
            sql += f" LIMIT {self.limit}"
        return sql


#: 파서가 반환하는 문장 노드
type Statement = InsertStmt | SelectStmt
