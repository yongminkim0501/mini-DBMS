"""바인더: AST + 카탈로그 → 바인딩된 문장 (이름 해석, 타입 검사, 값 변환).

파서는 스키마를 모르고 "문법적으로 맞는가"만 본다. 바인더는 카탈로그를 보고 "의미가 맞는가"를 판단한다:

- 테이블/컬럼이 존재하는가 (이름 → 스키마, 컬럼 인덱스)
- 값의 타입이 컬럼 타입과 맞는가, 맞지 않으면 PostgreSQL 의 대입 변환 규칙으로 바꿀 수 있는가
- INSERT 의 대상 컬럼과 값 개수, id(GENERATED ALWAYS) 규칙, NOT NULL

결과(BoundInsert / BoundSelect)에는 실행에 필요한 것만 남는다: 테이블 스키마, 컬럼 인덱스, 변환이 끝난 파이썬 값.
그래서 실행기는 이름이나 타입을 다시 확인하지 않는다. 바인딩은 실행 전에 **모든 행**에 대해 끝나므로
중간 행에서 실패하면 아무것도 쓰지 않는다.

모든 오류는 AST 의 position 으로 원문 위치를 표시한다.

**파라미터 ($n, 확장 쿼리 프로토콜의 준비된 문장)**: :class:`ParamContext` 를 넘기면 ``$n`` 의 타입을 쓰인 자리에서
추론하고, 값 자리에는 :class:`ParamRef` 를 남긴 "틀(template)" 을 만든다. 이름 해석·타입 검사는 틀을 만들 때
한 번만 하고, 실행할 때마다 :func:`bind_values` 가 ``ParamRef`` 를 값으로 바꾼 새 Bound 객체를 만든다
(파싱·바인딩 비용 없이 값만 바꿔 재사용 — 준비된 문장의 핵심). ``ParamContext`` 없이 ``$n`` 이 나오면
(단순 쿼리, CLI) ``42P02 there is no parameter $n``.

추론 규칙: INSERT 값 → 대상 컬럼 타입, WHERE 비교 → 상대 컬럼 타입, LIMIT → BIGINT.
클라이언트가 Parse 에서 타입 OID 를 지정했으면 그것을 쓴다 (int2/int4/int8 → BIGINT 계열, text/varchar → 문자열 계열,
0 과 unknown(705) → 자리에서 추론). 지정 타입과 컬럼 타입이 다르면 리터럴과 같은 규칙으로 변환한다
(문자열 → BIGINT 는 int8in, 정수 → TEXT 대입은 문자열로, ``text = integer`` 비교는 42883).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .catalog import BIGINT, TEXT, Catalog, TableSchema
from .errors import (
    AmbiguousParameter,
    DuplicateColumn,
    FeatureNotSupported,
    GeneratedAlways,
    IndeterminateDatatype,
    InvalidRowCountInLimitClause,
    InvalidSchemaName,
    InvalidTextRepresentation,
    NotNullViolation,
    NumericValueOutOfRange,
    SqlSyntaxError,
    UndefinedColumn,
    UndefinedFunction,
    UndefinedParameter,
    UndefinedTable,
)
from .result import ColumnInfo
from .sql.ast import (
    Condition,
    Default,
    ExplainStmt,
    InsertStmt,
    Literal,
    Param,
    QualifiedName,
    SelectStmt,
    SetStmt,
    ShowStmt,
    Star,
    Statement,
)
from .storage import BIGINT_MAX, BIGINT_MIN

__all__ = [
    "PARAM_TYPE_FAMILIES",
    "ParamRef",
    "ParamContext",
    "bind_values",
    "parse_integer_text",
    "statement_has_params",
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


# ------------------------------------------------------------------ 파라미터

#: 지원하는 파라미터 타입 OID → 계열 (카탈로그 타입). 0 과 unknown(705) 은 쓰인 자리에서 추론한다.
PARAM_TYPE_FAMILIES: dict[int, str] = {20: BIGINT, 21: BIGINT, 23: BIGINT, 25: TEXT, 1043: TEXT}
_UNSPECIFIED_OIDS = frozenset({0, 705})
_TYPE_NAMES: dict[int, str] = {20: "bigint", 21: "smallint", 23: "integer", 25: "text", 1043: "character varying"}
_FAMILY_OID: dict[str, int] = {BIGINT: 20, TEXT: 25}


@dataclass(frozen=True, slots=True)
class ParamRef:
    """바인딩 틀 안의 파라미터 자리. ``index`` 는 0부터 (``$1`` → 0). ``position`` 은 원문 위치 (값 변환 오류용)."""

    index: int
    position: int


class ParamContext:
    """준비된 문장 하나를 바인딩하는 동안 파라미터 타입을 모은다.

    Args:
        declared: Parse 메시지의 파라미터 타입 OID 목록 (없으면 빈 튜플). ``declared[i]`` 가 ``$i+1`` 의 타입.

    Raises:
        FeatureNotSupported: 지원하지 않는 타입 OID (0A000).
    """

    __slots__ = ("declared", "_types")

    def __init__(self, declared: Sequence[int] = ()) -> None:
        self.declared = tuple(declared)
        for i, oid in enumerate(self.declared):
            if oid not in _UNSPECIFIED_OIDS and oid not in PARAM_TYPE_FAMILIES:
                raise FeatureNotSupported(f"parameter ${i + 1} has unsupported data type (OID {oid})")
        self._types: dict[int, str] = {}  # 번호 → 계열 (쓰인 파라미터만)

    def _declared_oid(self, number: int) -> int:
        return self.declared[number - 1] if number <= len(self.declared) else 0

    def use(self, p: Param, context_type: str) -> tuple[ParamRef, str, str]:
        """``p`` 가 ``context_type`` (BIGINT/TEXT) 자리에 쓰였다. → (자리, 파라미터 계열, 오류 메시지용 타입 이름)."""
        n = p.number
        oid = self._declared_oid(n)
        if oid in _UNSPECIFIED_OIDS:
            prev = self._types.get(n)
            if prev is not None and prev != context_type:
                raise AmbiguousParameter(
                    f"inconsistent types deduced for parameter ${n}",
                    position=p.position,
                    detail=f"{_TYPE_NAMES[_FAMILY_OID[prev]]} versus {_TYPE_NAMES[_FAMILY_OID[context_type]]}",
                )
            family = context_type
            label = _TYPE_NAMES[_FAMILY_OID[family]]
        else:
            family = PARAM_TYPE_FAMILIES[oid]
            label = _TYPE_NAMES[oid]
        self._types[n] = family
        return ParamRef(n - 1, p.position), family, label

    def resolve(self) -> tuple[int, ...]:
        """모든 파라미터의 최종 타입 OID (ParameterDescription 과 Bind 의 값 해석에 쓴다).

        개수는 지정된 타입 수와 쓰인 가장 큰 번호 중 큰 쪽. 지정되지 않았고 쓰이지도 않은 번호(예: ``$2`` 만 씀)나
        추론할 수 없는 번호는 42P18.
        """
        count = max(len(self.declared), max(self._types, default=0))
        out: list[int] = []
        for n in range(1, count + 1):
            oid = self._declared_oid(n)
            if oid in _UNSPECIFIED_OIDS:
                family = self._types.get(n)
                if family is None:
                    raise IndeterminateDatatype(f"could not determine data type of parameter ${n}")
                oid = _FAMILY_OID[family]
            out.append(oid)
        return tuple(out)


def _no_params(p: Param) -> UndefinedParameter:
    return UndefinedParameter(f"there is no parameter ${p.number}", position=p.position)


# ------------------------------------------------------------------ 바인딩 결과


@dataclass(frozen=True, slots=True)
class BoundCondition:
    """``row[column_index] op value`` 비교 하나 (값은 컬럼 타입으로 변환 완료).

    ``value`` 가 None 이면 SQL NULL 과의 비교이므로 결과는 항상 UNKNOWN (어떤 행도 통과하지 못함).
    바인딩 틀에서는 :class:`ParamRef` 일 수 있다 (:func:`bind_values` 가 값으로 바꾼다).
    ``source`` 는 원래 리터럴 또는 파라미터 (EXPLAIN 표시용: ``'25'::bigint``, ``$1``).
    """

    column_index: int
    column_name: str
    column_type: str
    op: str
    value: Any
    source: Literal | Param


@dataclass(frozen=True, slots=True)
class BoundSelect:
    """바인딩된 SELECT.

    Attributes:
        table: 대상 테이블.
        columns: 출력할 컬럼의 인덱스 (전체 행 기준, id=0). 중복 허용.
        column_infos: 출력 컬럼 정보.
        where: AND 로 연결된 조건들.
        limit: 행 수 제한 (None 이면 없음). 틀에서는 ParamRef 일 수 있다.
    """

    table: TableSchema
    columns: tuple[int, ...]
    column_infos: tuple[ColumnInfo, ...]
    where: tuple[BoundCondition, ...]
    limit: int | ParamRef | None


@dataclass(frozen=True, slots=True)
class BoundInsert:
    """바인딩된 INSERT.

    Attributes:
        table: 대상 테이블.
        rows: data_columns 순서의 값 튜플들 (id 제외, 변환·NOT NULL 검사 완료). TableFile.append_many 에 그대로 넘긴다.
        returning: RETURNING 컬럼 인덱스 (전체 행 기준). RETURNING 이 없으면 None.
        returning_infos: RETURNING 컬럼 정보.
        param_slots: 틀에서 파라미터가 있는 자리 ``(행 번호, data 컬럼 번호, ParamRef)``. 값이 없으면 빈 튜플.
    """

    table: TableSchema
    rows: tuple[tuple[Any, ...], ...]
    returning: tuple[int, ...] | None
    returning_infos: tuple[ColumnInfo, ...]
    param_slots: tuple[tuple[int, int, ParamRef], ...] = ()


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


def bind(stmt: Statement, catalog: Catalog, params: ParamContext | None = None) -> BoundStatement:
    """AST 를 카탈로그로 해석해 바인딩된 문장을 만든다.

    ``params`` 가 있으면 ``$n`` 을 허용하고 타입을 추론해 ``params`` 에 기록한다 (결과는 :func:`bind_values` 로 값을
    채워야 실행할 수 있는 틀). 없으면 ``$n`` 은 42P02.

    Raises:
        InvalidSchemaName, UndefinedTable, UndefinedColumn, DuplicateColumn, GeneratedAlways,
        NumericValueOutOfRange, InvalidTextRepresentation, NotNullViolation, UndefinedFunction,
        SqlSyntaxError: 의미 오류. ``position`` 은 AST 의 원문 위치.
    """
    if isinstance(stmt, ExplainStmt):
        return BoundExplain(_bind_plain(stmt.statement, catalog, params), stmt.analyze)
    if isinstance(stmt, SetStmt):
        v = stmt.value
        if isinstance(v, Default):
            return BoundSet(stmt.name, None, stmt.name_position, v.position)
        return BoundSet(stmt.name, v.value, stmt.name_position, v.position)
    if isinstance(stmt, ShowStmt):
        return BoundShow(stmt.name, stmt.name_position)
    return _bind_plain(stmt, catalog, params)


def _bind_plain(
    stmt: InsertStmt | SelectStmt, catalog: Catalog, params: ParamContext | None
) -> BoundInsert | BoundSelect:
    if isinstance(stmt, InsertStmt):
        return _bind_insert(stmt, catalog, params)
    return _bind_select(stmt, catalog, params)


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


_INT_RANGES: dict[int, tuple[str, int, int]] = {
    20: ("bigint", BIGINT_MIN, BIGINT_MAX),
    23: ("integer", _INT4_MIN, _INT4_MAX),
    21: ("smallint", -(2**15), 2**15 - 1),
}


def parse_integer_text(text: str, type_oid: int = 20, position: int | None = None) -> int:
    """정수 타입(int2/int4/int8)의 텍스트 입력을 PostgreSQL 입력 함수 규칙으로 해석한다 (Bind 의 텍스트 형식 파라미터).

    Raises:
        InvalidTextRepresentation: 정수 형식이 아님 (22P02).
        NumericValueOutOfRange: 타입 범위 밖 (22003).
    """
    name, lo, hi = _INT_RANGES[type_oid]
    m = _BIGINT_TEXT_RE.match(text)
    if m is None:
        raise InvalidTextRepresentation(f'invalid input syntax for type {name}: "{text}"', position=position)
    value = int(m.group(1))
    if not lo <= value <= hi:
        raise NumericValueOutOfRange(f'value "{text}" is out of range for type {name}', position=position)
    return value


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


def _bind_insert(stmt: InsertStmt, catalog: Catalog, params: ParamContext | None) -> BoundInsert:
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
    slots: list[tuple[int, int, ParamRef]] = []
    for row_no, row in enumerate(stmt.rows):
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
            if isinstance(val, Param):
                if params is None:
                    raise _no_params(val)
                ref, _family, _label = params.use(val, types[k])
                data[k - 1] = ref  # 값 변환과 NOT NULL 검사는 bind_values 에서
                slots.append((row_no, k - 1, ref))
                continue
            data[k - 1] = _assign(val, types[k])
        # NOT NULL 검사: 명시한 NULL/DEFAULT 와 생략한 컬럼 모두 (파라미터 자리는 값이 정해진 뒤에).
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
        infos = tuple(_column_info(schema, k) for k in returning)

    return BoundInsert(schema, tuple(bound_rows), returning, infos, tuple(slots))


def _column_info(schema: TableSchema, k: int) -> ColumnInfo:
    c = schema.columns[k]
    return ColumnInfo.for_type(c.name, c.type, table_oid=schema.oid, column_number=k + 1)


# ------------------------------------------------------------------ SELECT


def _bind_select(stmt: SelectStmt, catalog: Catalog, params: ParamContext | None) -> BoundSelect:
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
    infos = tuple(_column_info(schema, k) for k in out)

    where = tuple(_bind_condition(c, schema, index, params) for c in stmt.where)
    limit: int | ParamRef | None
    if isinstance(stmt.limit, Param):
        if params is None:
            raise _no_params(stmt.limit)
        limit = params.use(stmt.limit, BIGINT)[0]
    else:
        limit = stmt.limit
    return BoundSelect(schema, out, infos, where, limit)


def _bind_condition(
    cond: Condition, schema: TableSchema, index: dict[str, int], params: ParamContext | None
) -> BoundCondition:
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
    if isinstance(lit, Param):
        if params is None:
            raise _no_params(lit)
        ref, family, label = params.use(lit, col.type)
        if col.type == TEXT and family == BIGINT:
            left, right = ("text", label) if not cond.literal_first else (label, "text")
            raise UndefinedFunction(
                f"operator does not exist: {left} {cond.written_op} {right}",
                position=cond.op_position or cond.position,
                hint=_OPERATOR_HINT,
            )
        return BoundCondition(k, col.name, col.type, cond.op, ref, lit)
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


# ------------------------------------------------------------------ 파라미터 값 채우기


def statement_has_params(bound: BoundStatement) -> bool:
    """바인딩 틀에 파라미터 자리가 있는가."""
    if isinstance(bound, BoundExplain):
        return statement_has_params(bound.statement)
    if isinstance(bound, BoundInsert):
        return bool(bound.param_slots)
    if isinstance(bound, BoundSelect):
        return isinstance(bound.limit, ParamRef) or any(isinstance(c.value, ParamRef) for c in bound.where)
    return False


def _param_to_bigint(v: int | str, ref: ParamRef) -> int:
    if isinstance(v, int):
        return v
    return _text_to_bigint(v, ref.position)


def bind_values(bound: BoundStatement, values: Sequence[int | str | None]) -> BoundStatement:
    """틀의 :class:`ParamRef` 를 값으로 바꾼 새 Bound 객체를 만든다 (틀은 그대로라 다시 쓸 수 있다).

    ``values[i]`` 는 ``$i+1`` 의 값으로, 파라미터 타입 계열에 맞게 이미 해석된 파이썬 값이다
    (BIGINT 계열 → int, 문자열 계열 → str, NULL → None). 컬럼 타입과 다르면 리터럴과 같은 규칙으로 변환한다.

    Raises:
        InvalidTextRepresentation, NumericValueOutOfRange: 문자열 값을 BIGINT 로 바꿀 수 없음.
        NotNullViolation: NOT NULL 컬럼에 NULL 파라미터.
        InvalidRowCountInLimitClause: LIMIT 파라미터가 음수.
    """
    if isinstance(bound, BoundExplain):
        inner = bind_values(bound.statement, values)
        return bound if inner is bound.statement else BoundExplain(inner, bound.analyze)  # type: ignore[arg-type]
    if isinstance(bound, BoundSelect):
        return _fill_select(bound, values)
    if isinstance(bound, BoundInsert):
        return _fill_insert(bound, values)
    return bound


def _fill_select(bound: BoundSelect, values: Sequence[int | str | None]) -> BoundSelect:
    if not statement_has_params(bound):
        return bound
    where = []
    for c in bound.where:
        ref = c.value
        if not isinstance(ref, ParamRef):
            where.append(c)
            continue
        v = values[ref.index]
        if v is not None and c.column_type == BIGINT:
            v = _param_to_bigint(v, ref)
        # TEXT 컬럼 vs BIGINT 계열 파라미터는 틀을 만들 때 42883 으로 거부했으므로 여기 오는 값은 str
        where.append(BoundCondition(c.column_index, c.column_name, c.column_type, c.op, v, c.source))
    limit = bound.limit
    if isinstance(limit, ParamRef):
        lv = values[limit.index]
        if lv is not None:
            lv = _param_to_bigint(lv, limit)
            if lv < 0:
                raise InvalidRowCountInLimitClause("LIMIT must not be negative")
        limit = lv  # NULL 이면 LIMIT 없음 (PostgreSQL 의 LIMIT NULL 과 같다)
    return BoundSelect(bound.table, bound.columns, bound.column_infos, tuple(where), limit)


def _fill_insert(bound: BoundInsert, values: Sequence[int | str | None]) -> BoundInsert:
    if not bound.param_slots:
        return bound
    cols = bound.table.columns
    rows = [list(r) for r in bound.rows]
    for row_no, j, ref in bound.param_slots:
        col = cols[j + 1]
        v = values[ref.index]
        if v is None:
            if not col.nullable:
                raise NotNullViolation(
                    f'null value in column "{col.name}" of relation "{bound.table.name}" violates not-null constraint',
                    position=ref.position,
                )
        elif col.type == BIGINT:
            v = _param_to_bigint(v, ref)
        elif not isinstance(v, str):
            v = str(v)  # 정수 → TEXT 대입 변환
        rows[row_no][j] = v
    return BoundInsert(bound.table, tuple(tuple(r) for r in rows), bound.returning, bound.returning_infos)
