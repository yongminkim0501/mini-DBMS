"""바인더: AST + 카탈로그 → 바인딩된 문장 (이름 해석, 타입 검사, 값 변환).

파서는 스키마를 모르고 "문법적으로 맞는가"만 본다. 바인더는 카탈로그를 보고 "의미가 맞는가"를 판단한다:

- 테이블/컬럼이 존재하는가 (이름 → 스키마, 컬럼 인덱스)
- 값의 타입이 컬럼 타입과 맞는가, 맞지 않으면 PostgreSQL 의 대입 변환 규칙으로 바꿀 수 있는가
- INSERT 의 대상 컬럼과 값 개수, id(GENERATED ALWAYS) 규칙, NOT NULL

결과(BoundInsert / BoundSelect)에는 실행에 필요한 것만 남는다: 테이블 스키마, 컬럼 인덱스, 변환이 끝난 파이썬 값.
그래서 실행기는 이름이나 타입을 다시 확인하지 않는다. 바인딩은 실행 전에 **모든 행**에 대해 끝나므로
중간 행에서 실패하면 아무것도 쓰지 않는다.

모든 오류는 AST 의 position 으로 원문 위치를 표시한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .catalog import BIGINT, Catalog, TableSchema
from .errors import (
    DuplicateColumn,
    GeneratedAlways,
    InvalidSchemaName,
    InvalidTextRepresentation,
    NotNullViolation,
    NumericValueOutOfRange,
    SqlSyntaxError,
    UndefinedColumn,
    UndefinedFunction,
    UndefinedTable,
)
from .result import ColumnInfo
from .sql.ast import (
    Condition,
    Default,
    ExplainStmt,
    InsertStmt,
    Literal,
    QualifiedName,
    SelectStmt,
    SetStmt,
    ShowStmt,
    Star,
    Statement,
)
from .storage import BIGINT_MAX, BIGINT_MIN

__all__ = [
    "BoundCondition",
    "BoundInsert",
    "BoundSelect",
    "BoundExplain",
    "BoundSet",
    "BoundShow",
    "BoundStatement",
    "bind",
]

# PostgreSQL int8in 이 허용하는 형식: 앞뒤 공백(isspace), 부호, ASCII 숫자.
# (int() 는 '1_000', 전각 숫자 '１２' 등도 받아들이므로 직접 검사한다.)
_BIGINT_TEXT_RE = re.compile(r"[ \t\n\r\v\f]*([+-]?[0-9]+)[ \t\n\r\v\f]*\Z")
_INT4_MIN, _INT4_MAX = -(2**31), 2**31 - 1

_OPERATOR_HINT = "No operator matches the given name and argument types. You might need to add explicit type casts."
_IDENTITY_DETAIL = 'Column "id" is an identity column defined as GENERATED ALWAYS.'


# ------------------------------------------------------------------ 바인딩 결과


@dataclass(frozen=True, slots=True)
class BoundCondition:
    """``row[column_index] op value`` 비교 하나 (값은 컬럼 타입으로 변환 완료).

    ``value`` 가 None 이면 SQL NULL 과의 비교이므로 결과는 항상 UNKNOWN (어떤 행도 통과하지 못함).
    ``source`` 는 원래 리터럴 (EXPLAIN 표시용: ``'25'::bigint`` 처럼 원문 형태를 보여준다).
    """

    column_index: int
    column_name: str
    column_type: str
    op: str
    value: Any
    source: Literal


@dataclass(frozen=True, slots=True)
class BoundSelect:
    """바인딩된 SELECT.

    Attributes:
        table: 대상 테이블.
        columns: 출력할 컬럼의 인덱스 (전체 행 기준, id=0). 중복 허용.
        column_infos: 출력 컬럼 정보.
        where: AND 로 연결된 조건들.
        limit: 행 수 제한 (None 이면 없음).
    """

    table: TableSchema
    columns: tuple[int, ...]
    column_infos: tuple[ColumnInfo, ...]
    where: tuple[BoundCondition, ...]
    limit: int | None


@dataclass(frozen=True, slots=True)
class BoundInsert:
    """바인딩된 INSERT.

    Attributes:
        table: 대상 테이블.
        rows: data_columns 순서의 값 튜플들 (id 제외, 변환·NOT NULL 검사 완료). TableFile.append_many 에 그대로 넘긴다.
        returning: RETURNING 컬럼 인덱스 (전체 행 기준). RETURNING 이 없으면 None.
        returning_infos: RETURNING 컬럼 정보.
    """

    table: TableSchema
    rows: tuple[tuple[Any, ...], ...]
    returning: tuple[int, ...] | None
    returning_infos: tuple[ColumnInfo, ...]


@dataclass(frozen=True, slots=True)
class BoundExplain:
    """바인딩된 EXPLAIN [ANALYZE]."""

    statement: BoundInsert | BoundSelect
    analyze: bool


@dataclass(frozen=True, slots=True)
class BoundSet:
    """``SET name = value``. 이름/값 검증은 실행 시 세션이 한다 (설정은 카탈로그가 아니라 세션의 것이므로).

    ``value`` 가 None 이면 ``SET name TO DEFAULT``.
    """

    name: str
    value: int | str | None
    name_position: int
    value_position: int


@dataclass(frozen=True, slots=True)
class BoundShow:
    """``SHOW name``."""

    name: str
    name_position: int


type BoundStatement = BoundInsert | BoundSelect | BoundExplain | BoundSet | BoundShow


# ------------------------------------------------------------------ 진입점


def bind(stmt: Statement, catalog: Catalog) -> BoundStatement:
    """AST 를 카탈로그로 해석해 바인딩된 문장을 만든다.

    Raises:
        InvalidSchemaName, UndefinedTable, UndefinedColumn, DuplicateColumn, GeneratedAlways,
        NumericValueOutOfRange, InvalidTextRepresentation, NotNullViolation, UndefinedFunction,
        SqlSyntaxError: 의미 오류. ``position`` 은 AST 의 원문 위치.
    """
    if isinstance(stmt, ExplainStmt):
        return BoundExplain(_bind_plain(stmt.statement, catalog), stmt.analyze)
    if isinstance(stmt, SetStmt):
        v = stmt.value
        if isinstance(v, Default):
            return BoundSet(stmt.name, None, stmt.name_position, v.position)
        return BoundSet(stmt.name, v.value, stmt.name_position, v.position)
    if isinstance(stmt, ShowStmt):
        return BoundShow(stmt.name, stmt.name_position)
    return _bind_plain(stmt, catalog)


def _bind_plain(stmt: InsertStmt | SelectStmt, catalog: Catalog) -> BoundInsert | BoundSelect:
    if isinstance(stmt, InsertStmt):
        return _bind_insert(stmt, catalog)
    return _bind_select(stmt, catalog)


# ------------------------------------------------------------------ 공통


def _resolve_table(name: QualifiedName, catalog: Catalog) -> TableSchema:
    """``[schema.]name`` → 테이블 스키마. 스키마는 생략 또는 ``public`` 만 존재한다."""
    if name.schema is not None and name.schema != "public":
        raise InvalidSchemaName(f'schema "{name.schema}" does not exist', position=name.position)
    try:
        return catalog.get(name.name)
    except UndefinedTable:
        # PostgreSQL 은 원문에 쓴 형태(public.x 포함)로 이름을 보여준다.
        shown = name.name if name.schema is None else f"{name.schema}.{name.name}"
        raise UndefinedTable(f'relation "{shown}" does not exist', position=name.position) from None


def _column_index(schema: TableSchema) -> dict[str, int]:
    return {c.name: i for i, c in enumerate(schema.columns)}


def _type_label(value: Any) -> str:
    """리터럴의 PostgreSQL 타입 이름 (정수 리터럴은 크기에 따라 integer / bigint / numeric)."""
    if isinstance(value, str):
        return "unknown"
    if _INT4_MIN <= value <= _INT4_MAX:
        return "integer"
    if BIGINT_MIN <= value <= BIGINT_MAX:
        return "bigint"
    return "numeric"


def _text_to_bigint(text: str, position: int) -> int:
    """문자열 → BIGINT (PostgreSQL int8in 규칙: 앞뒤 공백 허용, 10진 정수만)."""
    m = _BIGINT_TEXT_RE.match(text)
    if m is None:
        raise InvalidTextRepresentation(f'invalid input syntax for type bigint: "{text}"', position=position)
    value = int(m.group(1))
    if not BIGINT_MIN <= value <= BIGINT_MAX:
        raise NumericValueOutOfRange(f'value "{text}" is out of range for type bigint', position=position)
    return value


def _assign(lit: Literal, col_type: str) -> Any:
    """INSERT 의 대입 변환 (PostgreSQL assignment cast).

    BIGINT ← 정수 (범위 검사) / 문자열 (int8in 으로 해석)
    TEXT   ← 문자열 (그대로) / 정수 (문자열로, int → text 대입 변환)
    """
    v = lit.value
    if v is None:
        return None
    if col_type == BIGINT:
        if isinstance(v, int):
            if not BIGINT_MIN <= v <= BIGINT_MAX:
                raise NumericValueOutOfRange("bigint out of range", position=lit.position)
            return v
        return _text_to_bigint(v, lit.position)
    return v if isinstance(v, str) else str(v)


# ------------------------------------------------------------------ INSERT


def _bind_insert(stmt: InsertStmt, catalog: Catalog) -> BoundInsert:
    schema = _resolve_table(stmt.table, catalog)
    index = _column_index(schema)
    cols = schema.columns

    # 대상 컬럼: 목록이 있으면 그 순서, 없으면 PostgreSQL 처럼 테이블의 모든 컬럼(id 포함) 순서.
    if stmt.columns is not None:
        targets: list[int] = []
        seen: set[int] = set()
        for name, pos in zip(stmt.columns, stmt.column_positions or [stmt.position] * len(stmt.columns)):
            k = index.get(name)
            if k is None:
                raise UndefinedColumn(
                    f'column "{name}" of relation "{schema.name}" does not exist', position=pos
                )
            if k in seen:
                raise DuplicateColumn(f'column "{name}" specified more than once', position=pos)
            seen.add(k)
            targets.append(k)
    else:
        targets = list(range(len(cols)))

    # 값 개수 검사 (파서가 모든 행의 길이가 같음을 보장하므로 첫 행만 보면 된다).
    width = len(stmt.rows[0])
    if width > len(targets):
        extra = stmt.rows[0][len(targets)]
        raise SqlSyntaxError("INSERT has more expressions than target columns", position=extra.position)
    if width < len(targets) and stmt.columns is not None:
        pos = stmt.column_positions[width] if stmt.column_positions else stmt.position
        raise SqlSyntaxError("INSERT has more target columns than expressions", position=pos)
    # 컬럼 목록이 없고 값이 적으면 나머지는 기본값 (id 는 자동 부여, 나머지는 NULL).

    ncols = len(cols)
    nullable = [c.nullable for c in cols]
    types = [c.type for c in cols]
    bound_rows: list[tuple[Any, ...]] = []
    for row in stmt.rows:
        data: list[Any] = [None] * (ncols - 1)  # data_columns 순서, 기본값 NULL
        explicit_pos: list[int | None] = [None] * (ncols - 1)
        for k, val in zip(targets, row):
            if k == 0:
                # id 는 GENERATED ALWAYS AS IDENTITY 와 같다: DEFAULT 만 허용, 값은 저장 계층이 부여.
                if not isinstance(val, Default):
                    raise GeneratedAlways(
                        'cannot insert a non-DEFAULT value into column "id"',
                        position=val.position,
                        detail=_IDENTITY_DETAIL,
                    )
                continue
            explicit_pos[k - 1] = val.position
            if isinstance(val, Default):
                continue  # id 이외 컬럼의 DEFAULT 는 NULL
            data[k - 1] = _assign(val, types[k])
        # NOT NULL 검사: 명시한 NULL/DEFAULT 와 생략한 컬럼 모두.
        for j, v in enumerate(data):
            if v is None and not nullable[j + 1]:
                pos = explicit_pos[j] if explicit_pos[j] is not None else row[0].position
                raise NotNullViolation(
                    f'null value in column "{cols[j + 1].name}" of relation "{schema.name}" '
                    "violates not-null constraint",
                    position=pos,
                )
        bound_rows.append(tuple(data))

    returning: tuple[int, ...] | None = None
    infos: tuple[ColumnInfo, ...] = ()
    if isinstance(stmt.returning, Star):
        returning = tuple(range(ncols))
    elif stmt.returning is not None:
        idxs: list[int] = []
        for name, pos in zip(stmt.returning, stmt.returning_positions or [stmt.position] * len(stmt.returning)):
            k = index.get(name)
            if k is None:
                raise UndefinedColumn(f'column "{name}" does not exist', position=pos)
            idxs.append(k)
        returning = tuple(idxs)
    if returning is not None:
        infos = tuple(ColumnInfo.for_type(cols[k].name, cols[k].type) for k in returning)

    return BoundInsert(schema, tuple(bound_rows), returning, infos)


# ------------------------------------------------------------------ SELECT


def _bind_select(stmt: SelectStmt, catalog: Catalog) -> BoundSelect:
    schema = _resolve_table(stmt.table, catalog)
    index = _column_index(schema)
    cols = schema.columns

    if isinstance(stmt.columns, Star):
        out = tuple(range(len(cols)))
    else:
        idxs: list[int] = []
        for name, pos in zip(stmt.columns, stmt.column_positions or [stmt.position] * len(stmt.columns)):
            k = index.get(name)
            if k is None:
                raise UndefinedColumn(f'column "{name}" does not exist', position=pos)
            idxs.append(k)  # 같은 컬럼을 여러 번 선택해도 된다
        out = tuple(idxs)
    infos = tuple(ColumnInfo.for_type(cols[k].name, cols[k].type) for k in out)

    where = tuple(_bind_condition(c, schema, index) for c in stmt.where)
    return BoundSelect(schema, out, infos, where, stmt.limit)


def _bind_condition(cond: Condition, schema: TableSchema, index: dict[str, int]) -> BoundCondition:
    """WHERE 비교 하나의 타입 검사와 변환.

    BIGINT 컬럼 vs 정수 → 그대로 (bigint 범위 밖 정수도 비교 자체는 가능: PostgreSQL 의 bigint vs numeric)
    BIGINT 컬럼 vs 문자열 → BIGINT 로 변환 (실패 시 22P02)
    TEXT 컬럼 vs 문자열 → 그대로
    TEXT 컬럼 vs 정수 → 42883 operator does not exist (text 와 integer 사이에는 비교 연산자가 없다)
    어떤 컬럼 vs NULL → 허용, 결과는 항상 UNKNOWN
    """
    k = index.get(cond.column)
    if k is None:
        raise UndefinedColumn(f'column "{cond.column}" does not exist', position=cond.position)
    col = schema.columns[k]
    lit = cond.value
    v = lit.value
    if v is None:
        value: Any = None
    elif col.type == BIGINT:
        value = v if isinstance(v, int) else _text_to_bigint(v, lit.position)
    elif isinstance(v, str):
        value = v
    else:
        # 원문에 쓴 순서와 연산자로 메시지를 만든다 (예: 5 < name → "integer < text").
        left, right = ("text", _type_label(v)) if not cond.literal_first else (_type_label(v), "text")
        raise UndefinedFunction(
            f"operator does not exist: {left} {cond.written_op} {right}",
            position=cond.op_position or cond.position,
            hint=_OPERATOR_HINT,
        )
    return BoundCondition(k, col.name, col.type, cond.op, value, lit)
