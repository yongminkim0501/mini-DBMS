"""실행기: 계획 노드와 이터레이터(Volcano) 모델, EXPLAIN 출력.

**이터레이터 모델**: 각 계획 노드는 ``__iter__`` 로 행을 하나씩 내보낸다. 상위 노드는 하위 노드에서 행을 *당겨(pull)*
쓴다. 행은 필요할 때만 만들어지고 노드 사이를 하나씩 흘러간다::

    Limit ──당김──▶ Project ──당김──▶ Filter ──당김──▶ SeqScan   ──▶ TableFile.scan()
                                                    └─ IndexScan ──▶ B+ 트리 search/range → TableFile.read_at()

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
from .table import TableHandle

__all__ = [
    "NodeStats",
    "PlanNode",
    "SeqScan",
    "IndexScan",
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
    """테이블 전체를 파일 순서대로 읽는다 (TableFile.scan). deleted 레코드는 저장 계층이 건너뛴다.

    스냅샷 읽기: 첫 행을 당길 때 테이블 공유 락을 **잠깐** 잡고 파일 끝 offset 을 기록한 뒤 바로 풀고,
    그 offset 이전만 락 없이 읽는다. 스캔 도중 커밋된 INSERT 는 보이지 않는다 (문장 단위 일관성).
    """

    def __init__(self, handle: TableHandle, schema: TableSchema) -> None:
        super().__init__()
        self.handle = handle
        self.schema = schema
        #: 실제로 저장 계층에서 읽어 올린 행 수 (LIMIT 조기 종료 검증, Rows Removed by Filter 계산용)
        self.rows_scanned = 0

    @property
    def table_file(self) -> TableFile:
        return self.handle.file

    def _rows(self) -> Iterator[tuple[Any, ...]]:
        end = self.handle.snapshot_end()  # [LOCK:read] 짧게
        scan = self.handle.file.scan(end)
        try:
            for _offset, row in scan:
                self.rows_scanned += 1
                yield row
        finally:
            scan.close()  # type: ignore[attr-defined]


class IndexScan(PlanNode):
    """id 인덱스(B+ 트리)로 찾은 offset 의 레코드만 ``TableFile.read_at`` 으로 읽는다.

    - 점 검색 (``point`` 가 있으면): ``search(point)`` 한 번 → ``read_at`` 한 번 → 행 0~1 개.
    - 범위 검색 (클러스터드 범위 스캔): 트리에서 **하한 이상인 첫 (id, offset)** 만 찾고, 거기서부터
      ``TableFile.scan_from`` 으로 파일을 순차로 읽다가 id 가 상한을 넘으면 멈춘다. 파일 안의 id 는 엄격히 증가하므로
      (열 때 판정 6, append 의 순차 id) offset 순서 = id 순서이고, 그래서 시작 위치 이후의 레코드는 id 순으로 이어진다.
      행마다 랜덤 읽기(read_at)를 하지 않으므로 범위가 커져도 Seq Scan 보다 느려지지 않는다.
      상위 Limit 이 멈추면 scan_from 제너레이터도 닫혀 더 이상 읽지 않는다.
    - ``empty`` 면 (모순 조건, BIGINT 범위 밖 상수) 트리도 파일도 건드리지 않고 끝낸다.
    결과는 id 오름차순이다 (파일 순서와 같다). 상위 Filter 가 id 조건을 포함한 모든 조건을 다시 검사한다.

    Attributes:
        index_conds: EXPLAIN 의 ``Index Cond:`` 에 표시할 원래 조건들 (병합 전, 원문 순서).
        rows_fetched: 인덱스에서 찾아 실제로 읽은 행 수 (Rows Removed by Filter 계산용).
    """

    def __init__(
        self,
        handle: TableHandle,
        schema: TableSchema,
        *,
        index_conds: Sequence[BoundCondition],
        point: int | None = None,
        lo: int | None = None,
        lo_inclusive: bool = True,
        hi: int | None = None,
        hi_inclusive: bool = True,
        empty: bool = False,
    ) -> None:
        super().__init__()
        self.handle = handle
        self.schema = schema
        self.index_name = handle.index_name
        self.index_conds = tuple(index_conds)
        self.point = point
        self.lo, self.lo_inclusive = lo, lo_inclusive
        self.hi, self.hi_inclusive = hi, hi_inclusive
        self.empty = empty
        self.rows_fetched = 0

    def _rows(self) -> Iterator[tuple[Any, ...]]:
        if self.empty:
            return
        # 스냅샷 읽기: 트리 조회와 파일 끝 기록만 공유 락 안에서(짧게), 레코드 읽기는 락 밖에서.
        if self.point is not None:
            offset, end = self.handle.point_lookup(self.point)  # [LOCK:read]
            if offset is None or offset >= end:
                return
            row = self.handle.file.read_at(offset)
            if row is not None:
                self.rows_fetched += 1
                yield row
            return
        start, end = self.handle.range_start(self.lo, self.hi, self.lo_inclusive, self.hi_inclusive)  # [LOCK:read]
        if start is None:
            return
        hi = self.hi
        hi_inc = self.hi_inclusive
        rows = self.handle.file.scan_from(start, end)
        try:
            for _offset, row in rows:
                rid = row[0]
                if hi is not None and (rid > hi or (rid == hi and not hi_inc)):
                    return  # id 가 상한을 넘었다: 이후 레코드는 모두 더 크다
                self.rows_fetched += 1
                yield row
        finally:
            rows.close()  # type: ignore[attr-defined]


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
        self, child: PlanNode, handle: TableHandle, schema: TableSchema, returning: Sequence[int] | None
    ) -> None:
        super().__init__()
        self.child = child
        self.children = (child,)
        self.handle = handle
        self.schema = schema
        self.returning = None if returning is None else tuple(returning)
        self.inserted = 0

    def _rows(self) -> Iterator[tuple[Any, ...]]:
        rows = list(self.child)
        # [LOCK:write] 테이블 배타 락: 파일 append 와 인덱스 갱신이 한 구간 (TableHandle.append_many 안에서).
        placed = self.handle.append_many(rows)
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
        conds = node.conditions
        if isinstance(scan, IndexScan):
            # 인덱스 조건은 Index Cond: 로, 나머지만 Filter: 로 (PostgreSQL 과 같은 모양).
            # 실행 시 Filter 노드는 id 조건까지 모두 다시 검사하지만, 인덱스가 이미 보장한 조건이라 결과는 같다.
            used = {id(c) for c in scan.index_conds}
            details.append(f"Index Cond: {format_condition(scan.index_conds)}")
            if scan.point is None:
                details.append("Heap Fetch: sequential")
            conds = tuple(c for c in conds if id(c) not in used)
            fetched = scan.rows_fetched
        else:
            fetched = scan.rows_scanned if isinstance(scan, SeqScan) else None
        if conds:
            details.append(f"Filter: {format_condition(conds)}")
            if analyze and fetched is not None and node.stats is not None:
                details.append(f"Rows Removed by Filter: {fetched - node.stats.rows}")
        children = scan.children
    else:
        label = _label(node)
        if isinstance(node, IndexScan):  # WHERE 가 있으면 보통 Filter 아래에 있다
            if node.index_conds:
                details.append(f"Index Cond: {format_condition(node.index_conds)}")
            if node.point is None:
                details.append("Heap Fetch: sequential")

    prefix = "" if depth == 0 else " " * (6 * (depth - 1) + 2) + "->  "
    out.append(prefix + label + _actual(stats))
    indent = " " * (6 * depth + 2)
    out.extend(indent + d for d in details)
    for c in children:
        _render(c, depth + 1, out, analyze)


def _label(node: PlanNode) -> str:
    if isinstance(node, SeqScan):
        return f"Seq Scan on {quote_ident(node.schema.name)}"
    if isinstance(node, IndexScan):
        return f"Index Scan using {quote_ident(node.index_name)} on {quote_ident(node.schema.name)}"
    if isinstance(node, Limit):
        return "Limit"
    if isinstance(node, Insert):
        return f"Insert on {quote_ident(node.schema.name)}"
    if isinstance(node, ValuesScan):
        # PostgreSQL: 한 행이면 Result, 여러 행이면 Values Scan
        return "Result" if len(node.values) == 1 else 'Values Scan on "*VALUES*"'
    return type(node).__name__
