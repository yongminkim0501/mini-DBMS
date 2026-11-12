"""실행기: 계획 노드와 이터레이터(Volcano) 모델, EXPLAIN 출력.

**이터레이터 모델**: 각 계획 노드는 ``__iter__`` 로 행을 하나씩 내보낸다. 상위 노드는 하위 노드에서 행을 *당겨(pull)*
쓴다. 행은 필요할 때만 만들어지고 노드 사이를 하나씩 흘러간다::

    Limit ──당김──▶ Project ──당김──▶ Filter ──당김──▶ SeqScan ──▶ TableFile.scan()

``Limit`` 이 n 개를 내보낸 뒤 더 이상 당기지 않으면, 아래 노드의 제너레이터는 다음 행을 만들지 않고 멈춘 채로
남는다. 그래서 100만 건 테이블의 ``LIMIT 10`` 은 10개(조건이 있으면 조건을 통과한 10개)를 찾을 만큼만 읽고 끝난다.
상위 노드는 끝날 때 하위 이터레이터를 ``close()`` 해서 스캔에 쓰던 파일 핸들도 바로 정리한다.

EXPLAIN ANALYZE 는 각 노드에 :class:`NodeStats` 를 붙여 실제 행 수와 시간(하위 노드를 포함한 inclusive 시간,
PostgreSQL 과 같은 방식)을 잰다. 계측이 꺼진 일반 실행에서는 측정 코드가 실행 경로에 끼어들지 않는다.
"""

from __future__ import annotations

import operator
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from .binder import BoundCondition
from .catalog import BIGINT, TableSchema
from .sql.ast import quote_ident, quote_literal
from .storage import TableFile

__all__ = [
    "NodeStats",
    "PlanNode",
    "SeqScan",
    "Filter",
    "Project",
    "Limit",
    "ValuesScan",
    "Insert",
    "explain_lines",
    "format_condition",
]

_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "=": operator.eq,
    "<>": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}


@dataclass(slots=True)
class NodeStats:
    """EXPLAIN ANALYZE 계측값 (시간은 초 단위, 하위 노드 포함)."""

    rows: int = 0
    startup: float = 0.0  # 첫 행을 내보낼 때까지 걸린 시간
    total: float = 0.0  # 마지막 행까지(또는 끝날 때까지) 걸린 시간
    loops: int = 0


class PlanNode:
    """계획 노드의 공통 부모. 하위 클래스는 :meth:`_rows` 제너레이터만 구현한다."""

    children: tuple[PlanNode, ...] = ()

    def __init__(self) -> None:
        self.stats: NodeStats | None = None

    def __iter__(self) -> Iterator[tuple[Any, ...]]:
        if self.stats is None:
            return self._rows()
        return self._instrumented(self._rows())

    def _rows(self) -> Iterator[tuple[Any, ...]]:
        raise NotImplementedError

    def _instrumented(self, it: Iterator[tuple[Any, ...]]) -> Iterator[tuple[Any, ...]]:
        """행을 하나 당길 때마다 걸린 시간을 더한다. 하위 노드의 일도 next() 안에서 일어나므로 inclusive 시간이 된다."""
        st = self.stats
        assert st is not None
        st.loops += 1
        clock = time.perf_counter
        try:
            while True:
                t0 = clock()
                try:
                    row = next(it)
                except StopIteration:
                    st.total += clock() - t0
                    return
                st.total += clock() - t0
                if st.rows == 0:
                    st.startup = st.total
                st.rows += 1
                yield row
        finally:
            it.close()  # type: ignore[attr-defined]

    def enable_instrumentation(self) -> None:
        """이 노드와 모든 하위 노드에 계측을 켠다 (EXPLAIN ANALYZE)."""
        self.stats = NodeStats()
        for c in self.children:
            c.enable_instrumentation()


# ------------------------------------------------------------------ 스캔


class SeqScan(PlanNode):
    """테이블 전체를 파일 순서대로 읽는다 (TableFile.scan). deleted 레코드는 저장 계층이 건너뛴다."""

    def __init__(self, table_file: TableFile, schema: TableSchema) -> None:
        super().__init__()
        self.table_file = table_file
        self.schema = schema
        #: 실제로 저장 계층에서 읽어 올린 행 수 (LIMIT 조기 종료 검증, Rows Removed by Filter 계산용)
        self.rows_scanned = 0

    def _rows(self) -> Iterator[tuple[Any, ...]]:
        scan = self.table_file.scan()
        try:
            for _offset, row in scan:
                self.rows_scanned += 1
                yield row
        finally:
            scan.close()  # type: ignore[attr-defined]


class ValuesScan(PlanNode):
    """INSERT 의 VALUES 목록 (바인딩이 끝난 행들)을 내보낸다."""

    def __init__(self, rows: Sequence[tuple[Any, ...]]) -> None:
        super().__init__()
        self.values = rows

    def _rows(self) -> Iterator[tuple[Any, ...]]:
        yield from self.values


# ------------------------------------------------------------------ 행 가공


class Filter(PlanNode):
    """AND 로 연결된 비교를 모두 만족하는 행만 통과시킨다.

    NULL 의미론 (SQL 3치 논리): NULL 과의 비교는 UNKNOWN 이고, WHERE 는 TRUE 인 행만 통과시킨다.
    AND 에서 하나라도 FALSE/UNKNOWN 이면 그 행은 제외된다.
    - 컬럼 값이 NULL 인 행 → 그 조건은 UNKNOWN → 제외
    - 리터럴이 NULL 인 조건 (``age = NULL``) → 모든 행에서 UNKNOWN → 어떤 행도 통과하지 못한다
      (PostgreSQL 은 이런 조건을 계획 단계에서 상수 false 로 접는다. 여기서는 스캔 없이 바로 끝낸다.)
    TEXT 비교는 파이썬 str 비교 = 유니코드 코드포인트 순서 (PostgreSQL 의 "C" collation 과 같다).
    """

    def __init__(self, child: PlanNode, conditions: Sequence[BoundCondition]) -> None:
        super().__init__()
        self.child = child
        self.children = (child,)
        self.conditions = tuple(conditions)
        self._compiled = tuple((c.column_index, _OPS[c.op], c.value) for c in conditions)
        self.always_unknown = any(c.value is None for c in conditions)

    def _rows(self) -> Iterator[tuple[Any, ...]]:
        if self.always_unknown:
            return
        conds = self._compiled
        it = iter(self.child)
        try:
            if len(conds) == 1:
                ((i, fn, v),) = conds
                for row in it:
                    x = row[i]
                    if x is not None and fn(x, v):
                        yield row
                return
            for row in it:
                for i, fn, v in conds:
                    x = row[i]
                    if x is None or not fn(x, v):
                        break
                else:
                    yield row
        finally:
            it.close()  # type: ignore[attr-defined]


class Project(PlanNode):
    """출력 컬럼만 골라 낸다 (중복·순서 변경 허용). 전체 컬럼을 순서대로 고르면 그대로 통과."""

    def __init__(self, child: PlanNode, columns: Sequence[int], ncols: int) -> None:
        super().__init__()
        self.child = child
        self.children = (child,)
        self.columns = tuple(columns)
        self.identity = self.columns == tuple(range(ncols))

    def _rows(self) -> Iterator[tuple[Any, ...]]:
        it = iter(self.child)
        try:
            if self.identity:
                yield from it
                return
            cols = self.columns
            if len(cols) == 1:
                (c,) = cols
                for row in it:
                    yield (row[c],)
                return
            pick = operator.itemgetter(*cols)
            for row in it:
                yield pick(row)
        finally:
            it.close()  # type: ignore[attr-defined]


class Limit(PlanNode):
    """처음 n 개만 내보낸다. n 개에 도달하면 **하위 노드를 더 당기지 않고** 즉시 끝난다."""

    def __init__(self, child: PlanNode, count: int) -> None:
        super().__init__()
        self.child = child
        self.children = (child,)
        self.count = count

    def _rows(self) -> Iterator[tuple[Any, ...]]:
        n = self.count
        if n <= 0:
            return  # LIMIT 0: 하위 노드를 아예 시작하지 않는다
        it = iter(self.child)
        try:
            for row in it:
                yield row
                n -= 1
                if n == 0:
                    # 여기서 return 하면 하위 제너레이터에 다음 행을 요청하지 않는다 → 스캔이 멈춘다.
                    return
        finally:
            it.close()  # type: ignore[attr-defined]


# ------------------------------------------------------------------ INSERT


class Insert(PlanNode):
    """하위 노드(ValuesScan)의 행을 ``append_many`` **한 번**으로 쓴다 (원자적: 실패하면 아무것도 안 쓴다).

    RETURNING 이 있으면 부여된 id 를 포함한 행에서 RETURNING 컬럼을 골라 내보낸다. 없으면 아무 행도 내보내지 않는다
    (PostgreSQL 의 Insert 노드와 같다). 삽입한 행 수는 :attr:`inserted`.
    """

    def __init__(
        self, child: PlanNode, table_file: TableFile, schema: TableSchema, returning: Sequence[int] | None
    ) -> None:
        super().__init__()
        self.child = child
        self.children = (child,)
        self.table_file = table_file
        self.schema = schema
        self.returning = None if returning is None else tuple(returning)
        self.inserted = 0

    def _rows(self) -> Iterator[tuple[Any, ...]]:
        rows = list(self.child)
        # [LOCK:write] Phase 3: 테이블 쓰기. TableFile.append_many 자체가 배타 락 구간이다.
        placed = self.table_file.append_many(rows)
        self.inserted = len(placed)
        if self.returning is None:
            return
        cols = self.returning
        for (rid, _off), values in zip(placed, rows):
            full = (rid, *values)
            yield tuple(full[c] for c in cols)


# ------------------------------------------------------------------ EXPLAIN


def _format_value(cond: BoundCondition) -> str:
    """EXPLAIN 에 표시할 비교 값. PostgreSQL 처럼 타입이 확정된 상수에 ``::type`` 을 붙인다."""
    src = cond.source.value
    type_name = "bigint" if cond.column_type == BIGINT else "text"
    if src is None:
        return f"NULL::{type_name}"
    if isinstance(src, int):
        return str(src)  # 정수 리터럴 (TEXT 컬럼과는 바인더가 이미 거부)
    return f"{quote_literal(src)}::{type_name}"  # 문자열 리터럴 → 컬럼 타입으로 변환된 상수


def format_condition(conds: Sequence[BoundCondition]) -> str:
    """``Filter:`` 줄의 식. 하나면 ``(age > 30)``, 여럿이면 ``((age > 30) AND (name = 'kim'::text))``."""
    parts = [f"({quote_ident(c.column_name)} {c.op} {_format_value(c)})" for c in conds]
    return parts[0] if len(parts) == 1 else "(" + " AND ".join(parts) + ")"


def _actual(st: NodeStats | None) -> str:
    if st is None:
        return ""
    return f"  (actual time={st.startup * 1000:.3f}..{st.total * 1000:.3f} rows={st.rows} loops={max(st.loops, 1)})"


def explain_lines(root: PlanNode, *, analyze: bool = False, execution_ms: float | None = None) -> list[str]:
    """계획 트리를 PostgreSQL EXPLAIN 텍스트 형식(단순화)으로 만든다.

    표시 규칙 (PostgreSQL 과 같은 모양):
    - Project 는 표시하지 않는다 (PostgreSQL 은 스캔 노드의 target list 로 처리).
    - Filter 는 별도 노드가 아니라 바로 아래 스캔 노드의 ``Filter:`` 속성으로 표시한다.
      ANALYZE 의 rows 는 필터를 통과한 행 수이고, ``Rows Removed by Filter`` 를 덧붙인다.
    - 하위 노드는 ``->  `` 로 들여 쓴다.
    """
    out: list[str] = []
    _render(root, 0, out, analyze)
    if analyze and execution_ms is not None:
        out.append(f"Execution Time: {execution_ms:.3f} ms")
    return out


def _render(node: PlanNode, depth: int, out: list[str], analyze: bool) -> None:
    # Project 는 건너뛰고 그 아래를 같은 깊이에 표시
    while isinstance(node, Project):
        node = node.child

    details: list[str] = []
    stats = node.stats if analyze else None
    children: tuple[PlanNode, ...] = node.children

    if isinstance(node, Filter):
        scan = node.child
        while isinstance(scan, Project):
            scan = scan.child
        label = _label(scan)
        details.append(f"Filter: {format_condition(node.conditions)}")
        if analyze and isinstance(scan, SeqScan) and node.stats is not None:
            details.append(f"Rows Removed by Filter: {scan.rows_scanned - node.stats.rows}")
        children = scan.children
    else:
        label = _label(node)

    prefix = "" if depth == 0 else " " * (6 * (depth - 1) + 2) + "->  "
    out.append(prefix + label + _actual(stats))
    indent = " " * (6 * depth + 2)
    out.extend(indent + d for d in details)
    for c in children:
        _render(c, depth + 1, out, analyze)


def _label(node: PlanNode) -> str:
    if isinstance(node, SeqScan):
        return f"Seq Scan on {quote_ident(node.schema.name)}"
    if isinstance(node, Limit):
        return "Limit"
    if isinstance(node, Insert):
        return f"Insert on {quote_ident(node.schema.name)}"
    if isinstance(node, ValuesScan):
        # PostgreSQL: 한 행이면 Result, 여러 행이면 Values Scan
        return "Result" if len(node.values) == 1 else 'Values Scan on "*VALUES*"'
    return type(node).__name__
