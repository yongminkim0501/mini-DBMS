"""Database: 외부(CLI, Phase 3 HTTP 서버)가 쓰는 단일 진입점. **여러 스레드에서 동시에 써도 안전하다.**

파이프라인::

    SQL 텍스트 ─iter_statements─▶ StatementSource ─parse_statement─▶ AST ─bind─▶ Bound*
               ─plan_select / plan_insert─▶ 계획 트리 ─이터레이터 실행─▶ QueryResult

테이블은 :class:`~minidb.table.TableHandle` (데이터 파일 + id B+ 트리 인덱스 ``<table>_pkey``)로 연다.
인덱스는 열 때의 파일 검증 스캔 한 번으로 벌크 로딩된다 (디스크에 저장하지 않는다).

설정(``enable_indexscan`` 등)은 :class:`~minidb.session.Session` 에 있다. ``execute(sql, session)`` 에 세션을 넘기지 않으면
Database 의 기본 세션(:attr:`Database.session`)을 쓴다. **세션은 스레드 안전하지 않다**: 여러 스레드에서 쓸 때는
스레드(연결)마다 Session 을 넘긴다 (HTTP 서버는 요청 또는 HTTP 세션마다 하나).

락 (docs/server.md "잠금 설계"):
- ``_tables_lock`` (일반 Lock): 테이블 핸들 캐시 생성. 이중 확인 패턴 — 이미 열린 핸들은 락 없이 dict 에서 꺼내고,
  없을 때만 락을 잡고 다시 확인한 뒤 연다. 두 스레드가 같은 테이블을 동시에 처음 열어 핸들이 두 개 생기면
  각자 next_id 와 인덱스를 들고 append 해 id 가 겹치므로, 생성은 반드시 락 안에서 한 번만.
  (락 없는 조회는 dict 의 get 이 스레드 안전하다는 CPython 의 보장 — free-threaded 빌드에서는 dict 내부 락 — 에
  기댄다. 핸들은 완전히 만든 뒤에만 dict 에 넣는다.)
- 테이블마다 RW 락 (:class:`~minidb.table.TableHandle`): SELECT 는 스냅샷을 잡는 동안만 공유 락,
  INSERT 는 파일 append + 인덱스 갱신 전체를 배타 락.
- 카탈로그는 생성 시 한 번 로드한 뒤 바꾸지 않는다 (읽기 전용 → 락 불필요). :meth:`reload_catalog` 는 다른 스레드가
  실행 중이 아닐 때만 부른다 (서버는 부르지 않는다).
"""

from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

from .binder import (
    BoundExplain,
    BoundInsert,
    BoundSelect,
    BoundSet,
    BoundShow,
    BoundStatement,
    ParamContext,
    bind,
    bind_values,
)
from .catalog import Catalog, TableSchema
from .errors import DatabaseError, SqlSyntaxError
from .executor import PlanNode, explain_lines
from .index import DEFAULT_ORDER
from .planner import plan_insert, plan_select
from .result import ColumnInfo, QueryResult
from .session import SETTINGS, Session
from .sql.ast import Statement
from .sql.lexer import StatementSource, iter_statements
from .sql.parser import parse_statement
from .storage import TableFile
from .table import TableHandle

__all__ = ["Database", "PreparedStatement", "result_columns"]

_EXPLAIN_COLUMNS = [ColumnInfo("QUERY PLAN", "text", 25)]


@dataclass(frozen=True, slots=True)
class PreparedStatement:
    """준비된 문장 (wire protocol 의 Parse 결과). 불변이라 여러 번 실행해도 안전하다.

    Attributes:
        sql: 원문 (오류 위치의 기준).
        statement: 파싱 결과. 빈 쿼리면 None (Execute 하면 EmptyQueryResponse).
        bound: 바인딩 틀 (``$n`` 자리는 ParamRef). 빈 쿼리면 None.
        param_types: 파라미터 타입 OID (``$1`` 부터, ParameterDescription).
        columns: 결과 컬럼 (RowDescription). 결과 행이 없는 문장이면 None (NoData).
    """

    sql: str
    statement: Statement | None
    bound: BoundStatement | None
    param_types: tuple[int, ...]
    columns: list[ColumnInfo] | None


def result_columns(bound: BoundStatement) -> list[ColumnInfo] | None:
    """문장을 실행하지 않고 결과 컬럼을 알아낸다 (Describe). 결과 행이 없는 문장이면 None."""
    if isinstance(bound, BoundSelect):
        return list(bound.column_infos)
    if isinstance(bound, BoundInsert):
        return list(bound.returning_infos) if bound.returning is not None else None
    if isinstance(bound, BoundExplain):
        return list(_EXPLAIN_COLUMNS)
    if isinstance(bound, BoundShow):
        d = SETTINGS.get(bound.name.lower())
        return [ColumnInfo(d.name if d is not None else bound.name, "text", 25)]
    return None


class Database:
    """``db_dir`` (``schema/``, ``data/``) 하나를 여는 데이터베이스.

    - 생성 시 카탈로그를 **즉시** 로드한다. 지연 로드를 하면 Phase 3 에서 여러 스레드가 동시에 첫 조회를 할 때
      적재가 겹치는 경쟁 조건이 생긴다.
    - 테이블은 처음 쓸 때 열어(파일 검증 + 인덱스 구성) 캐시하고, :meth:`close` 에서 모두 닫는다.
    - 이번 단계에서는 스레드 안전하지 않다.

    Args:
        db_dir: ``schema/``, ``data/`` 가 있는 디렉터리.
        max_text_bytes: TEXT 값 하나의 최대 바이트 수.
        index_order: id 인덱스 B+ 트리의 order (벤치마크용).
    """

    def __init__(
        self,
        db_dir: str | Path,
        *,
        max_text_bytes: int = 1_048_576,
        index_order: int = DEFAULT_ORDER,
        lock_timeout: float | None = None,
    ) -> None:
        self.db_dir = Path(db_dir)
        self.max_text_bytes = max_text_bytes
        self.index_order = index_order
        self.lock_timeout = lock_timeout
        self.catalog = Catalog(self.db_dir)
        self.catalog.reload()  # 즉시 로드 (스키마 오류도 여기서 드러난다). 이후 읽기 전용.
        self._tables_lock = threading.Lock()
        self._tables: dict[int, TableHandle] = {}  # 쓰기는 _tables_lock 아래에서만
        self._closed = False
        #: ``execute`` 에 세션을 넘기지 않았을 때 쓰는 세션
        self.session = Session()

    # ------------------------------------------------------------ 테이블
    def table_handle(self, schema: TableSchema) -> TableHandle:
        """테이블의 TableHandle(파일 + 인덱스)을 (처음이면 열어서) 반환한다."""
        h = self._tables.get(schema.oid)  # 빠른 경로: 이미 열린 핸들 (락 없이)
        if h is not None:
            return h
        with self._tables_lock:  # [LOCK] 느린 경로: 다시 확인하고 한 번만 연다
            if self._closed:
                raise ValueError("Database is closed")
            h = self._tables.get(schema.oid)
            if h is None:
                h = TableHandle.open(
                    self.catalog.data_path(schema.name),
                    schema,
                    max_text_bytes=self.max_text_bytes,
                    order=self.index_order,
                    lock_timeout=self.lock_timeout,
                )
                self._tables[schema.oid] = h  # 완전히 만든 뒤에 공개
            return h

    def table_file(self, schema: TableSchema) -> TableFile:
        """테이블의 TableFile (``table_handle(schema).file``)."""
        return self.table_handle(schema).file

    def reload_catalog(self) -> None:
        """스키마 디렉터리를 다시 읽는다. 열린 테이블은 닫는다 (스키마가 바뀌었을 수 있으므로).

        다른 스레드가 문장을 실행 중이 아닐 때만 부른다 (카탈로그는 읽기 전용이라는 가정을 잠시 깬다).
        """
        with self._tables_lock:
            self._close_tables_locked()
            self.catalog.reload()

    # ------------------------------------------------------------ 실행
    def execute(self, sql: str, session: Session | None = None) -> Iterator[QueryResult | DatabaseError]:
        """세미콜론으로 구분된 SQL 을 한 문장씩 실행해 결과(또는 오류)를 차례로 내보낸다.

        오류가 나도 다음 문장을 계속 실행할지는 호출자가 정한다 (다음 원소를 요청하지 않으면 멈춘다).
        오류의 position 은 ``sql`` 전체 기준이다. SELECT 결과는 스트리밍이므로 다음 원소를 요청하기 전에 행을 읽는다.
        ``session`` 을 생략하면 :attr:`session` (기본 세션)을 쓴다.
        """
        return self.execute_sources(iter_statements(sql), session)

    def execute_sources(
        self, sources: Iterable[StatementSource], session: Session | None = None
    ) -> Iterator[QueryResult | DatabaseError]:
        """이미 분리된 문장들을 실행한다 (CLI 의 스트리밍 입력이 이 경로를 쓴다)."""
        for src in sources:
            try:
                yield self.execute_one(src, session)
            except DatabaseError as exc:
                yield exc

    def execute_one(self, source: StatementSource | str, session: Session | None = None) -> QueryResult:
        """문장 하나를 실행한다. 오류는 예외로 올라온다 (position 은 source 의 원문 기준)."""
        stmt = parse_statement(source)
        bound = bind(stmt, self.catalog)
        return self.execute_bound(bound, session)

    def prepare(self, sql: str, param_types: Sequence[int] = ()) -> PreparedStatement:
        """문장 하나를 파싱·바인딩해 재사용할 수 있는 :class:`PreparedStatement` 를 만든다 (wire protocol 의 Parse).

        ``param_types`` 는 ``$1`` 부터의 타입 OID (0 이면 추론). 카탈로그는 읽기 전용이므로 바인딩 결과를 계속 써도 된다.

        Raises:
            SqlSyntaxError: 문장이 둘 이상 (``cannot insert multiple commands into a prepared statement``) 또는 구문 오류.
            IndeterminateDatatype, AmbiguousParameter: 파라미터 타입을 정할 수 없음.
            DatabaseError: 그 밖의 파싱·바인딩 오류 (position 은 ``sql`` 기준).
        """
        sources = list(itertools.islice(iter_statements(sql), 2))
        if len(sources) > 1:
            raise SqlSyntaxError("cannot insert multiple commands into a prepared statement")
        ctx = ParamContext(param_types)
        if not sources:
            return PreparedStatement(sql, None, None, ctx.resolve(), None)
        stmt = parse_statement(sources[0])
        bound = bind(stmt, self.catalog, ctx)
        return PreparedStatement(sql, stmt, bound, ctx.resolve(), result_columns(bound))

    def execute_prepared(
        self, prepared: PreparedStatement, values: Sequence[int | str | None] = (), session: Session | None = None
    ) -> QueryResult:
        """준비된 문장을 파라미터 값으로 실행한다 (파싱·바인딩 없이 값만 채운다).

        ``values`` 는 파라미터 타입에 맞게 해석된 값 (BIGINT 계열 int, 문자열 계열 str, NULL None).
        """
        if prepared.bound is None:
            raise ValueError("empty prepared statement has nothing to execute")
        if len(values) != len(prepared.param_types):
            raise ValueError(f"expected {len(prepared.param_types)} parameter values, got {len(values)}")
        return self.execute_bound(bind_values(prepared.bound, values), session)

    def execute_bound(self, bound: BoundStatement, session: Session | None = None) -> QueryResult:
        """바인딩된 문장을 계획·실행한다 (벤치마크가 단계별 시간을 잴 때 직접 부른다)."""
        sess = self.session if session is None else session
        if isinstance(bound, BoundExplain):
            return self._explain(bound, sess)
        if isinstance(bound, BoundInsert):
            return self._insert(bound, sess)
        if isinstance(bound, BoundSet):
            sess.set(
                bound.name, bound.value, name_position=bound.name_position, value_position=bound.value_position
            )
            return QueryResult(None, [], "SET")
        if isinstance(bound, BoundShow):
            column, value = sess.show(bound.name, position=bound.name_position)
            return QueryResult([ColumnInfo(column, "text", 25)], [(value,)], "SHOW")
        return self._select(bound, sess)

    def _select(self, bound: BoundSelect, session: Session) -> QueryResult:
        # [LOCK:read] Phase 3: 테이블 공유 락. TableFile.scan 은 생성 시점의 끝까지만 읽으므로 이후 append 와 섞이지 않는다.
        plan = plan_select(bound, self.table_handle, session)
        return QueryResult(list(bound.column_infos), iter(plan), tag_prefix="SELECT")

    def _insert(self, bound: BoundInsert, session: Session) -> QueryResult:
        # [LOCK:write] Phase 3: 테이블 배타 락 (Insert 노드의 TableHandle.append_many: 파일 + 인덱스).
        node = plan_insert(bound, self.table_handle, session)
        rows = list(node)  # 실제 삽입은 여기서 일어난다 (RETURNING 이 없으면 행은 비어 있다)
        columns = list(bound.returning_infos) if bound.returning is not None else None
        return QueryResult(columns, rows, f"INSERT 0 {node.inserted}")

    def _plan(self, bound: BoundInsert | BoundSelect, session: Session) -> PlanNode:
        if isinstance(bound, BoundInsert):
            return plan_insert(bound, self.table_handle, session)
        return plan_select(bound, self.table_handle, session)

    def _explain(self, bound: BoundExplain, session: Session) -> QueryResult:
        plan = self._plan(bound.statement, session)
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
        """열린 모든 테이블을 flush 한다 (``durable`` 이면 fsync). 테이블마다 배타 락."""
        with self._tables_lock:
            handles = list(self._tables.values())
        for h in handles:
            h.flush(durable=durable)  # [LOCK:write]

    def _close_tables_locked(self) -> None:
        tables, self._tables = self._tables, {}
        for h in tables.values():
            h.close()  # [LOCK:write] 진행 중인 INSERT 가 끝날 때까지 기다린다

    def close(self) -> None:
        """모든 테이블을 닫는다. 여러 번 불러도 안전. 이후 새 문장은 ValueError."""
        with self._tables_lock:
            if not self._closed:
                self._closed = True
                self._close_tables_locked()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()
