"""플래너: 바인딩된 문장 → 실행 계획 트리.

SELECT 계획의 모양은 항상 같다::

    [Limit] → Project → [Filter] → <접근 경로: SeqScan | IndexScan>

접근 경로(테이블에서 행을 꺼내는 방법)는 :func:`choose_access_path` **한 곳에서만** 정한다.
Filter 는 접근 경로와 관계없이 WHERE 의 모든 조건(id 조건 포함)을 다시 검사하므로, 접근 경로가 무엇이든 결과는 같다.
이 성질을 차등 테스트(enable_indexscan on/off 결과 비교)로 검증한다.

비용 모델(통계)은 없다. 규칙 기반: id 에 대한 유효한 경계가 하나라도 있으면 인덱스를 쓴다.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .binder import BoundCondition, BoundInsert, BoundSelect
from .catalog import TableSchema
from .executor import Filter, IndexScan, Insert, Limit, PlanNode, Project, SeqScan, ValuesScan
from .session import Session
from .storage import BIGINT_MAX, BIGINT_MIN
from .table import TableHandle

__all__ = ["TableProvider", "IdRange", "merge_id_conditions", "choose_access_path", "plan_select", "plan_insert"]

#: 테이블 스키마 → 열린 TableHandle (엔진이 캐시를 관리한다)
type TableProvider = Callable[[TableSchema], TableHandle]

ID_COLUMN = 0  # 카탈로그가 첫 컬럼을 항상 id BIGINT 자동 증가로 강제한다
_INDEXABLE_OPS = frozenset({"=", "<", "<=", ">", ">="})  # <> 는 B+ 트리 범위로 표현할 수 없다


@dataclass(frozen=True, slots=True)
class IdRange:
    """id 조건들을 합친 결과 범위. ``lo``/``hi`` 가 None 이면 그쪽은 무한.

    ``empty`` 면 어떤 행도 만족할 수 없다 (모순, 또는 BIGINT 범위 밖 상수와의 비교).
    """

    lo: int | None
    lo_inclusive: bool
    hi: int | None
    hi_inclusive: bool
    empty: bool = False

    @property
    def point(self) -> int | None:
        """``lo == hi`` 이고 양쪽 다 포함이면 그 값 (점 검색), 아니면 None."""
        if self.lo is not None and self.lo == self.hi and self.lo_inclusive and self.hi_inclusive:
            return self.lo
        return None

    @property
    def unbounded(self) -> bool:
        return not self.empty and self.lo is None and self.hi is None


def merge_id_conditions(conds: Sequence[BoundCondition]) -> tuple[IdRange | None, tuple[BoundCondition, ...]]:
    """AND 로 묶인 조건 중 인덱스로 쓸 수 있는 id 조건을 모아 하나의 범위로 합친다.

    - ``id = c`` 는 하한 c(포함)와 상한 c(포함) 두 조건으로 본다.
    - 하한은 가장 큰 값, 상한은 가장 작은 값을 고른다. 값이 같으면 미포함(더 엄격한 쪽)이 이긴다.
      예: ``id >= 5 AND id > 5`` → ``id > 5``, ``id <= 9 AND id < 9`` → ``id < 9``.
    - 하한 > 상한, 또는 같은 값인데 한쪽이라도 미포함이면 모순 → empty.
    - BIGINT 범위 밖 상수: 하한이 최댓값보다 크면(또는 = 최댓값인데 미포함) empty, 최솟값보다 작으면 하한 없음.
      상한도 대칭. 예: ``id < 2^70`` 은 상한 없음, ``id > 2^70`` 은 empty.
    - ``<>`` 와 NULL 비교는 제외한다 (Filter 가 처리).

    Returns:
        (범위, 사용한 조건들). 쓸 수 있는 id 조건이 없으면 (None, ()).
    """
    used = tuple(
        c for c in conds if c.column_index == ID_COLUMN and c.op in _INDEXABLE_OPS and c.value is not None
    )
    if not used:
        return None, ()
    lo: int | None = None
    lo_inc = True
    hi: int | None = None
    hi_inc = True
    for c in used:
        v = c.value
        if c.op in ("=", ">", ">="):
            inc = c.op != ">"
            if lo is None or v > lo or (v == lo and not inc):
                lo, lo_inc = v, inc
        if c.op in ("=", "<", "<="):
            inc = c.op != "<"
            if hi is None or v < hi or (v == hi and not inc):
                hi, hi_inc = v, inc

    empty = False
    # BIGINT 정의역으로 자르기
    if lo is not None:
        if lo > BIGINT_MAX or (lo == BIGINT_MAX and not lo_inc):
            empty = True
        elif lo < BIGINT_MIN:
            lo, lo_inc = None, True
    if hi is not None:
        if hi < BIGINT_MIN or (hi == BIGINT_MIN and not hi_inc):
            empty = True
        elif hi > BIGINT_MAX:
            hi, hi_inc = None, True
    if lo is not None and hi is not None and (lo > hi or (lo == hi and not (lo_inc and hi_inc))):
        empty = True
    return IdRange(lo, lo_inc, hi, hi_inc, empty), used


def choose_access_path(bound: BoundSelect, handle: TableHandle, session: Session | None = None) -> PlanNode:
    """테이블에서 행을 꺼내는 접근 경로 노드를 고른다.

    1. ``enable_indexscan = off`` → Seq Scan.
    2. NULL 과의 비교가 있으면 → Seq Scan (Filter 가 스캔을 시작하지도 않고 끝낸다. 조건 전체가 항상 거짓).
    3. id 조건(=, <, <=, >, >=)을 합친 범위가
       - 없음 (id 조건이 없거나 ``<>`` 뿐) 또는 양쪽 다 무한 (예: ``id < 2^70`` 만) → Seq Scan
       - 점 (``id = c``) → 점 검색 Index Scan
       - 그 외 → 범위 Index Scan (empty 면 Index Scan 이지만 실행 시 아무것도 읽지 않는다)
    """
    seq = SeqScan(handle.file, bound.table)
    if session is not None and not session.enable_indexscan:
        return seq
    if any(c.value is None for c in bound.where):
        return seq
    rng, used = merge_id_conditions(bound.where)
    if rng is None or rng.unbounded:
        return seq
    if rng.empty:
        return IndexScan(handle, bound.table, index_conds=used, empty=True)
    point = rng.point
    if point is not None:
        return IndexScan(handle, bound.table, index_conds=used, point=point)
    return IndexScan(
        handle,
        bound.table,
        index_conds=used,
        lo=rng.lo,
        lo_inclusive=rng.lo_inclusive,
        hi=rng.hi,
        hi_inclusive=rng.hi_inclusive,
    )


def plan_select(bound: BoundSelect, tables: TableProvider, session: Session | None = None) -> PlanNode:
    """``[Limit] → Project → [Filter] → 접근 경로`` 계획을 만든다."""
    node: PlanNode = choose_access_path(bound, tables(bound.table), session)
    if bound.where:
        node = Filter(node, bound.where)
    node = Project(node, bound.columns, len(bound.table.columns))
    if bound.limit is not None:
        node = Limit(node, bound.limit)
    return node


def plan_insert(bound: BoundInsert, tables: TableProvider) -> Insert:
    """``Insert → ValuesScan`` 계획을 만든다."""
    return Insert(ValuesScan(bound.rows), tables(bound.table), bound.table, bound.returning)
