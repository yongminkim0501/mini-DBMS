"""Database: 외부(CLI, 이후 Phase 3 서버)가 쓰는 단일 진입점.

파이프라인::

    SQL 텍스트 ─iter_statements─▶ StatementSource ─parse_statement─▶ AST ─bind─▶ Bound*
               ─plan_select / plan_insert─▶ 계획 트리 ─이터레이터 실행─▶ QueryResult

Phase 3 락 지점 (``# [LOCK]`` 주석):
- TableFile 캐시 생성 (:meth:`Database.table_file`): 두 스레드가 같은 테이블을 동시에 처음 열면 TableFile 이
  두 개 생기고 각자 next_id 를 들고 append 해 id 가 겹친다 → 캐시 조회·생성은 락 안에서.
- 카탈로그 reload (:meth:`Database.reload_catalog`): 읽는 스레드가 반쯤 갱신된 사전을 보면 안 된다.
- 쓰기 실행 (INSERT): TableFile.append_many 가 배타(write) 락 구간. 읽기(SELECT)는 공유(read) 락.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from types import TracebackType
from typing import Self

from .binder import BoundExplain, BoundInsert, BoundSelect, BoundStatement, bind
from .catalog import Catalog, TableSchema
from .errors import DatabaseError
from .executor import PlanNode, explain_lines
from .planner import plan_insert, plan_select
from .result import ColumnInfo, QueryResult
from .sql.lexer import StatementSource, iter_statements
from .sql.parser import parse_statement
from .storage import TableFile

__all__ = ["Database"]

_EXPLAIN_COLUMNS = [ColumnInfo("QUERY PLAN", "text", 25)]


class Database:
    """``db_dir`` (``schema/``, ``data/``) 하나를 여는 데이터베이스.

    - 생성 시 카탈로그를 **즉시** 로드한다. 지연 로드를 하면 Phase 3 에서 여러 스레드가 동시에 첫 조회를 할 때
      적재가 겹치는 경쟁 조건이 생긴다.
    - TableFile 은 테이블별로 처음 쓸 때 열어 캐시하고, :meth:`close` 에서 모두 닫는다.
    - 이번 단계에서는 스레드 안전하지 않다.
    """

    def __init__(self, db_dir: str | Path, *, max_text_bytes: int = 1_048_576) -> None:
        self.db_dir = Path(db_dir)
        self.max_text_bytes = max_text_bytes
        self.catalog = Catalog(self.db_dir)
        self.catalog.reload()  # 즉시 로드 (스키마 오류도 여기서 드러난다)
        self._tables: dict[int, TableFile] = {}
        self._closed = False

    # ------------------------------------------------------------ 테이블 파일
    def table_file(self, schema: TableSchema) -> TableFile:
        """테이블의 TableFile 을 (처음이면 열어서) 반환한다."""
        if self._closed:
            raise ValueError("Database is closed")
        # [LOCK] Phase 3: 캐시 조회와 생성을 한 임계 구역으로 (같은 테이블에 TableFile 이 두 개 생기면 id 가 겹친다).
        tf = self._tables.get(schema.oid)
        if tf is None:
            tf = TableFile(self.catalog.data_path(schema.name), schema, max_text_bytes=self.max_text_bytes)
            self._tables[schema.oid] = tf
        return tf

    def reload_catalog(self) -> None:
        """스키마 디렉터리를 다시 읽는다. 열린 TableFile 은 닫는다 (스키마가 바뀌었을 수 있으므로)."""
        # [LOCK] Phase 3: 배타 락. 실행 중인 문장이 없을 때만 바꿔야 한다.
        self.catalog.reload()
        self._close_tables()

    # ------------------------------------------------------------ 실행
    def execute(self, sql: str) -> Iterator[QueryResult | DatabaseError]:
        """세미콜론으로 구분된 SQL 을 한 문장씩 실행해 결과(또는 오류)를 차례로 내보낸다.

        오류가 나도 다음 문장을 계속 실행할지는 호출자가 정한다 (다음 원소를 요청하지 않으면 멈춘다).
        오류의 position 은 ``sql`` 전체 기준이다. SELECT 결과는 스트리밍이므로 다음 원소를 요청하기 전에 행을 읽는다.
        """
        return self.execute_sources(iter_statements(sql))

    def execute_sources(self, sources: Iterable[StatementSource]) -> Iterator[QueryResult | DatabaseError]:
        """이미 분리된 문장들을 실행한다 (CLI 의 스트리밍 입력이 이 경로를 쓴다)."""
        for src in sources:
            try:
                yield self.execute_one(src)
            except DatabaseError as exc:
                yield exc

    def execute_one(self, source: StatementSource | str) -> QueryResult:
        """문장 하나를 실행한다. 오류는 예외로 올라온다 (position 은 source 의 원문 기준)."""
        stmt = parse_statement(source)
        bound = bind(stmt, self.catalog)
        return self.execute_bound(bound)

    def execute_bound(self, bound: BoundStatement) -> QueryResult:
        """바인딩된 문장을 계획·실행한다 (벤치마크가 단계별 시간을 잴 때 직접 부른다)."""
        if isinstance(bound, BoundExplain):
            return self._explain(bound)
        if isinstance(bound, BoundInsert):
            return self._insert(bound)
        return self._select(bound)

    def _select(self, bound: BoundSelect) -> QueryResult:
        # [LOCK:read] Phase 3: 공유 락. TableFile.scan 은 생성 시점의 끝까지만 읽으므로 이후 append 와 섞이지 않는다.
        plan = plan_select(bound, self.table_file)
        return QueryResult(list(bound.column_infos), iter(plan), tag_prefix="SELECT")

    def _insert(self, bound: BoundInsert) -> QueryResult:
        # [LOCK:write] Phase 3: 배타 락 (Insert 노드의 append_many).
        node = plan_insert(bound, self.table_file)
        rows = list(node)  # 실제 삽입은 여기서 일어난다 (RETURNING 이 없으면 행은 비어 있다)
        columns = list(bound.returning_infos) if bound.returning is not None else None
        return QueryResult(columns, rows, f"INSERT 0 {node.inserted}")

    def _plan(self, bound: BoundInsert | BoundSelect) -> PlanNode:
        if isinstance(bound, BoundInsert):
            return plan_insert(bound, self.table_file)
        return plan_select(bound, self.table_file)

    def _explain(self, bound: BoundExplain) -> QueryResult:
        plan = self._plan(bound.statement)
        if not bound.analyze:
            return QueryResult(_EXPLAIN_COLUMNS, [(line,) for line in explain_lines(plan)], "EXPLAIN")
        # ANALYZE: 실제로 실행한다 (INSERT 도 실제로 삽입된다, PostgreSQL 과 동일). 결과 행은 버린다.
        plan.enable_instrumentation()
        t0 = time.perf_counter()
        for _ in plan:
            pass
        elapsed_ms = (time.perf_counter() - t0) * 1000
        lines = explain_lines(plan, analyze=True, execution_ms=elapsed_ms)
        return QueryResult(_EXPLAIN_COLUMNS, [(line,) for line in lines], "EXPLAIN")

    # ------------------------------------------------------------ 수명
    def flush(self, *, durable: bool = False) -> None:
        """열린 모든 TableFile 을 flush 한다 (``durable`` 이면 fsync)."""
        for tf in self._tables.values():
            tf.flush(durable=durable)

    def _close_tables(self) -> None:
        tables, self._tables = self._tables, {}
        for tf in tables.values():
            tf.close()

    def close(self) -> None:
        """모든 TableFile 을 닫는다. 여러 번 불러도 안전."""
        if not self._closed:
            self._close_tables()
            self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()
