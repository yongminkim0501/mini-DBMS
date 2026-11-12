"""플래너: 바인딩된 문장 → 실행 계획 트리.

SELECT 계획의 모양은 항상 같다::

    [Limit] → Project → [Filter] → <접근 경로>

접근 경로(테이블에서 행을 꺼내는 방법)는 :func:`choose_access_path` **한 곳에서만** 정한다.
Phase 1 에서는 항상 SeqScan 이다. Phase 2 에서 B+ 트리 인덱스가 생기면 이 함수에만 IndexScan 분기를 추가하면 되고,
나머지 노드(Filter/Project/Limit)와 실행기는 바꿀 필요가 없다.
"""

from __future__ import annotations

from collections.abc import Callable

from .binder import BoundInsert, BoundSelect
from .catalog import TableSchema
from .executor import Filter, Insert, Limit, PlanNode, Project, SeqScan, ValuesScan
from .storage import TableFile

__all__ = ["TableProvider", "choose_access_path", "plan_select", "plan_insert"]

#: 테이블 스키마 → 열린 TableFile (엔진이 캐시를 관리한다)
type TableProvider = Callable[[TableSchema], TableFile]


def choose_access_path(bound: BoundSelect, tables: TableProvider) -> PlanNode:
    """테이블에서 행을 꺼내는 접근 경로 노드를 고른다.

    Phase 1: 인덱스가 없으므로 항상 순차 스캔.

    Phase 2 확장 지점 ──────────────────────────────────────────────────────────────
    파서가 조건을 ``컬럼 op 리터럴`` 모양으로 정규화했으므로(``42 = id`` → ``id = 42``) 여기서는 한 가지 모양만 보면 된다::

        for cond in bound.where:
            if cond.column_name == "id" and cond.op == "=" and cond.value is not None:
                return IndexScan(index_for(bound.table), key=cond.value, table_file=tables(bound.table))

    IndexScan 은 B+ 트리에서 id → offset 을 찾아 ``TableFile.read_at(offset)`` 으로 행 하나를 내보낸다.
    조건 전체는 그대로 위의 Filter 가 다시 검사하므로(id 조건 포함) 결과의 정확성은 접근 경로와 무관하다.
    ────────────────────────────────────────────────────────────────────────────────
    """
    return SeqScan(tables(bound.table), bound.table)


def plan_select(bound: BoundSelect, tables: TableProvider) -> PlanNode:
    """``[Limit] → Project → [Filter] → 접근 경로`` 계획을 만든다."""
    node: PlanNode = choose_access_path(bound, tables)
    if bound.where:
        node = Filter(node, bound.where)
    node = Project(node, bound.columns, len(bound.table.columns))
    if bound.limit is not None:
        node = Limit(node, bound.limit)
    return node


def plan_insert(bound: BoundInsert, tables: TableProvider) -> Insert:
    """``Insert → ValuesScan`` 계획을 만든다."""
    return Insert(ValuesScan(bound.rows), tables(bound.table), bound.table, bound.returning)
