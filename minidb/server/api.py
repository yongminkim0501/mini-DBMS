"""HTTP API: 엔드포인트, /query 실행과 JSON 변환, 세션 저장소.

엔드포인트::

    POST   /query            본문: application/json {"sql": "..."} 또는 text/plain SQL. 헤더 X-Session-Id 로 세션 지정.
    POST   /sessions         → 201 {"session_id": "..."}
    DELETE /sessions/{id}    → 204, 없으면 404
    GET    /health           → {"status": "ok"}
    GET    /stats            → 풀/연결/세션 지표, 가동 시간, 파이썬 빌드 정보

/query 응답::

    {"results": [{"command": "SELECT 3", "columns": [{"name", "type", "type_oid"}], "rows": [[...]], "row_count": 3}],
     "error": null | {"sqlstate", "message", "detail", "hint", "position", "statement_index"}}

- 여러 문장을 순서대로 실행하고 **첫 오류에서 멈춘다**. 앞 문장의 결과(INSERT 등)는 이미 반영되어 있다(트랜잭션 없음).
- 상태 코드: 모두 성공 200, SQL 오류 400 (성공한 앞 문장 결과 포함), 내부 오류(SQLSTATE XX 클래스, 예상 못한 예외) 500.
- 결과 행이 ``max_result_rows`` 를 넘으면 54000 오류 (HINT: LIMIT).
- BIGINT 는 JSON 숫자다. 2^53 을 넘는 값은 JavaScript 의 Number 에서 정밀도가 깨질 수 있다 (docs/server.md).
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..engine import Database
from ..errors import DatabaseError, LockNotAvailable, ProgramLimitExceeded
from ..result import QueryResult
from ..session import Session
from .dispatcher import Response, error_body
from .http import Request

__all__ = ["Api", "SessionStore", "DEFAULT_MAX_RESULT_ROWS"]

DEFAULT_MAX_RESULT_ROWS = 100_000
_JSON = "application/json; charset=utf-8"


def _json(status: int, obj: Any, **kw: Any) -> Response:
    return Response(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), _JSON, **kw)


# ------------------------------------------------------------------ 세션


@dataclass(slots=True)
class SessionEntry:
    session: Session
    lock: threading.Lock = field(default_factory=threading.Lock)  # 같은 세션의 동시 요청을 직렬화
    last_used: float = field(default_factory=time.monotonic)


class SessionStore:
    """HTTP 세션(id → Session). 모든 접근은 ``self._lock`` 아래.

    - 세션 id 는 ``secrets.token_hex(16)`` (추측 불가). 인증은 아니다 (docs/server.md 보안상 한계).
    - ``timeout`` 초 동안 쓰이지 않은 세션은 만료된다 (접근할 때 함께 정리).
    """

    def __init__(self, timeout: float = 600.0, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.timeout = timeout
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, SessionEntry] = {}
        self._expired = 0

    def _sweep_locked(self, now: float) -> None:
        dead = [k for k, e in self._entries.items() if now - e.last_used > self.timeout and not e.lock.locked()]
        for k in dead:
            del self._entries[k]
        self._expired += len(dead)

    def create(self) -> str:
        sid = secrets.token_hex(16)
        with self._lock:
            now = self._clock()
            self._sweep_locked(now)
            self._entries[sid] = SessionEntry(Session(), last_used=now)
        return sid

    def get(self, sid: str) -> SessionEntry | None:
        with self._lock:
            now = self._clock()
            self._sweep_locked(now)
            e = self._entries.get(sid)
            if e is not None:
                e.last_used = now
            return e

    def touch(self, entry: SessionEntry) -> None:
        with self._lock:
            entry.last_used = self._clock()

    def delete(self, sid: str) -> bool:
        with self._lock:
            return self._entries.pop(sid, None) is not None

    def stats(self) -> dict[str, int]:
        with self._lock:
            self._sweep_locked(self._clock())
            return {"active": len(self._entries), "expired_total": self._expired}


# ------------------------------------------------------------------ 결과 → JSON


def error_json(err: DatabaseError, statement_index: int | None) -> dict[str, Any]:
    return {
        "sqlstate": err.sqlstate,
        "message": err.message,
        "detail": err.detail,
        "hint": err.hint,
        "position": err.position,
        "statement_index": statement_index,
    }


def result_json(result: QueryResult, max_rows: int) -> dict[str, Any]:
    """QueryResult 를 JSON 객체로. 행이 ``max_rows`` 를 넘으면 스캔을 멈추고 ProgramLimitExceeded."""
    if result.columns is None:
        tag = result.command_tag
        parts = tag.split()
        count = int(parts[-1]) if parts[-1].isdigit() and len(parts) > 1 else None
        return {"command": tag, "columns": None, "rows": None, "row_count": count}
    rows: list[list[Any]] = []
    it = iter(result.rows)
    try:
        for row in it:
            if len(rows) >= max_rows:
                raise ProgramLimitExceeded(
                    f"query result exceeds max_result_rows ({max_rows})",
                    hint="Add a LIMIT clause or a narrower WHERE condition.",
                )
            rows.append(list(row))
    finally:
        close = getattr(it, "close", None)
        if close is not None:
            close()  # 남은 스캔을 멈추고 파일 핸들을 닫는다
    tag = result.command_tag  # 행을 다 읽었으므로 태그가 확정되어 있다
    return {
        "command": tag,
        "columns": [{"name": c.name, "type": c.type_name, "type_oid": c.type_oid} for c in result.columns],
        "rows": rows,
        "row_count": len(rows),
    }


# ------------------------------------------------------------------ 라우팅


class Api:
    """요청 처리기. 워커 스레드 여러 개에서 동시에 불린다 (공유 상태: Database, SessionStore, 둘 다 스레드 안전)."""

    def __init__(
        self,
        db: Database,
        *,
        sessions: SessionStore | None = None,
        max_result_rows: int = DEFAULT_MAX_RESULT_ROWS,
        session_lock_timeout: float | None = 30.0,
        stats_provider: Callable[[], dict[str, Any]] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.db = db
        self.sessions = sessions or SessionStore()
        self.max_result_rows = max_result_rows
        self.session_lock_timeout = session_lock_timeout
        self.stats_provider = stats_provider or (lambda: {})
        self.log = log or (lambda m: None)

    def handle(self, req: Request) -> Response:
        path = req.path
        if path == "/query":
            return self._only(req, ("POST",)) or self.query(req)
        if path == "/health":
            return self._only(req, ("GET", "HEAD")) or _json(200, {"status": "ok"})
        if path == "/stats":
            return self._only(req, ("GET", "HEAD")) or _json(200, self.stats_provider())
        if path == "/sessions":
            return self._only(req, ("POST",)) or _json(201, {"session_id": self.sessions.create()})
        if path.startswith("/sessions/") and path.count("/") == 2 and len(path) > len("/sessions/"):
            bad = self._only(req, ("DELETE",))
            if bad:
                return bad
            sid = path[len("/sessions/") :]
            if self.sessions.delete(sid):
                return Response(204, b"", None)
            return _json(404, _http_error(404, f'session "{sid}" does not exist'))
        return _json(404, _http_error(404, f"no such endpoint: {path}"))

    @staticmethod
    def _only(req: Request, methods: tuple[str, ...]) -> Response | None:
        if req.method in methods:
            return None
        return _json(
            405, _http_error(405, f"method {req.method} not allowed"), headers={"Allow": ", ".join(methods)}
        )

    # ------------------------------------------------------------ /query
    def _sql_from_body(self, req: Request) -> str | Response:
        ctype = req.content_type
        charset = req.charset
        if charset is not None and charset not in ("utf-8", "utf8"):
            return _json(415, _http_error(415, f"unsupported charset: {charset}"))
        if ctype is None or ctype == "text/plain":
            try:
                return req.body.decode("utf-8")
            except UnicodeDecodeError:
                return _json(400, _http_error(400, "request body is not valid UTF-8", "22021"))
        if ctype == "application/json":
            try:
                obj = json.loads(req.body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                return _json(400, _http_error(400, f"invalid JSON body: {exc}"))
            if not isinstance(obj, dict) or not isinstance(obj.get("sql"), str):
                return _json(400, _http_error(400, 'JSON body must be an object with a string "sql" field'))
            return obj["sql"]
        return _json(415, _http_error(415, f"unsupported Content-Type: {ctype} (use application/json or text/plain)"))

    def query(self, req: Request) -> Response:
        sql = self._sql_from_body(req)
        if isinstance(sql, Response):
            return sql
        sid = req.header("x-session-id")
        if sid is None:
            return self._run(sql, Session())  # 요청마다 새 기본 세션
        entry = self.sessions.get(sid)
        if entry is None:
            return _json(404, _http_error(404, f'session "{sid}" does not exist'))
        # 같은 세션의 요청은 한 번에 하나씩 (Session 은 스레드 안전하지 않다)
        timeout = -1 if self.session_lock_timeout is None else self.session_lock_timeout
        if not entry.lock.acquire(timeout=timeout):
            err = LockNotAvailable(f'could not obtain lock on session "{sid}"')
            return _json(400, {"results": [], "error": error_json(err, None)})
        try:
            return self._run(sql, entry.session)
        finally:
            entry.lock.release()
            self.sessions.touch(entry)

    def _run(self, sql: str, session: Session) -> Response:
        results: list[dict[str, Any]] = []
        error: dict[str, Any] | None = None
        status = 200
        index = 0
        stream = self.db.execute(sql, session)
        try:
            for item in stream:
                if isinstance(item, DatabaseError):
                    error = error_json(item, index)
                    break
                try:
                    results.append(result_json(item, self.max_result_rows))
                except DatabaseError as exc:  # 행을 읽는 중의 오류 (54000, 락 시간 초과, 손상 …)
                    error = error_json(exc, index)
                    break
                index += 1
        except Exception as exc:  # noqa: BLE001 - 엔진 버그: 500 으로 응답하고 서버는 계속
            self.log(f"internal error while executing statement {index}: {exc!r}")
            err = {
                "sqlstate": "XX000",
                "message": f"internal error: {type(exc).__name__}: {exc}",
                "detail": None,
                "hint": None,
                "position": None,
                "statement_index": index,
            }
            return _json(500, {"results": results, "error": err})
        finally:
            stream.close()
        if error is not None:
            status = 500 if error["sqlstate"].startswith("XX") else 400
        return _json(status, {"results": results, "error": error})


def _http_error(status: int, message: str, sqlstate: str | None = None) -> dict[str, Any]:
    return json.loads(error_body(status, message, sqlstate))
