"""서버 조립: Database + WorkerPool + Dispatcher + Api (HTTP) / PgService (PostgreSQL wire protocol), 시작과 정상 종료.

HTTP 와 PG 리스너는 하나만 또는 둘 다 켤 수 있고, 켜면 같은 엔진·워커 풀·디스패처를 함께 쓴다.

정상 종료 순서 (SIGINT/SIGTERM 또는 :meth:`Server.shutdown`):

1. dispatcher 정지: 새 연결 수락 중단, 리스닝 소켓 닫기, 유휴 keep-alive 연결 닫기
   (유휴 PG 연결에는 FATAL 57P01 ``terminating connection due to administrator command`` 를 보낸 뒤 닫는다).
2. 워커 풀 graceful shutdown: 큐에 있는 요청과 처리 중인 요청을 끝까지 처리 (응답은 ``Connection: close``).
3. 모든 테이블 ``flush(durable=True)`` (fsync) 후 닫기.

두 번째 신호를 받으면 기다리지 않고 즉시 프로세스를 끝낸다(``os._exit``). 이미 끝난 INSERT 는 append 반환 시점에
OS 로 넘어가 있으므로(저장 계층 불변식 (a)) 프로세스 종료로 사라지지 않는다. fsync 만 건너뛴다.
"""

from __future__ import annotations

import os
import resource
import signal
import socket
import sys
import sysconfig
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..engine import Database
from ..pgwire.service import PgService
from .api import DEFAULT_MAX_RESULT_ROWS, Api, SessionStore
from .dispatcher import Dispatcher
from .http import MAX_BODY_BYTES, MAX_HEADER_BYTES
from .pool import WorkerPool

__all__ = ["ServerConfig", "Server", "runtime_info"]


@dataclass(slots=True)
class ServerConfig:
    host: str = "127.0.0.1"  # 인증이 없으므로 기본은 로컬만 (docs/server.md 보안상 한계)
    port: int | None = 8080  # HTTP. None 이면 HTTP 리스너 없음
    pg_port: int | None = None  # PostgreSQL wire protocol. None 이면 없음 (CLI 기본은 5433)
    pg_database: str = "minidb"  # PG 접속에서 허용하는 데이터베이스 이름
    pg_idle_timeout: float | None = None  # 시작 이후 유휴 PG 연결 제한 (None: 없음, PostgreSQL 기본과 같다)
    pg_startup_timeout: float = 60.0  # 시작 패킷 수신 제한 (PostgreSQL authentication_timeout)
    pg_send_timeout: float = 60.0
    workers: int = 4
    queue_size: int = 64
    max_connections: int = 256
    idle_timeout: float = 60.0
    request_timeout: float = 10.0
    max_header_bytes: int = MAX_HEADER_BYTES
    max_body_bytes: int = MAX_BODY_BYTES
    max_result_rows: int = DEFAULT_MAX_RESULT_ROWS
    session_timeout: float = 600.0
    lock_timeout: float | None = 30.0
    access_log: bool = True
    backlog: int = 512


def runtime_info() -> dict[str, Any]:
    fn = getattr(sys, "_is_gil_enabled", None)
    return {
        "python_version": sys.version.split()[0],
        "sys_version": sys.version,
        "free_threaded_build": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
        "gil_enabled": fn() if fn is not None else True,
        "cpu_count": os.cpu_count(),
        "pid": os.getpid(),
    }


class _Logger:
    """여러 워커가 동시에 쓰는 로그: 한 줄씩 락 아래에서 write (줄이 섞이지 않게)."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def __call__(self, line: str) -> None:
        with self._lock:
            try:
                self._stream.write(line + "\n")
                self._stream.flush()
            except (OSError, ValueError):
                pass


class Server:
    """minidb HTTP 서버.

    Args:
        db: 열린 Database 또는 DB 디렉터리 경로 (경로면 서버가 열고 닫는다).
        config: 설정.
        log_stream: 접근 로그와 오류 로그를 쓸 스트림 (기본 stderr).
    """

    def __init__(self, db: Database | str | Path, config: ServerConfig | None = None, *, log_stream: Any = None) -> None:
        self.config = config or ServerConfig()
        cfg = self.config
        self._owns_db = not isinstance(db, Database)
        self.db = Database(db, lock_timeout=cfg.lock_timeout) if self._owns_db else db  # type: ignore[arg-type]
        self.log = _Logger(log_stream if log_stream is not None else sys.stderr)
        self.pool = WorkerPool(cfg.workers, cfg.queue_size, log=self.log)
        self.sessions = SessionStore(cfg.session_timeout)
        self.api = Api(
            self.db,
            sessions=self.sessions,
            max_result_rows=cfg.max_result_rows,
            session_lock_timeout=cfg.lock_timeout,
            stats_provider=self.stats,
            log=self.log,
        )
        if cfg.port is None and cfg.pg_port is None:
            raise ValueError("at least one of port (HTTP) and pg_port must be set")
        self.listener: socket.socket | None = None
        self.address: tuple[str, int] | None = None
        if cfg.port is not None:
            self.listener = socket.create_server((cfg.host, cfg.port), backlog=cfg.backlog, reuse_port=False)
            self.address = self.listener.getsockname()[:2]
        self.pg: PgService | None = None
        self.pg_listener: socket.socket | None = None
        self.pg_address: tuple[str, int] | None = None
        if cfg.pg_port is not None:
            try:
                self.pg_listener = socket.create_server((cfg.host, cfg.pg_port), backlog=cfg.backlog, reuse_port=False)
            except OSError:
                if self.listener is not None:
                    self.listener.close()
                raise
            self.pg_address = self.pg_listener.getsockname()[:2]
            self.pg = PgService(
                self.db,
                database_name=cfg.pg_database,
                log=self.log,
                access_log=cfg.access_log,
                startup_timeout=cfg.pg_startup_timeout,
                idle_timeout=cfg.pg_idle_timeout,
                send_timeout=cfg.pg_send_timeout,
            )
        self.dispatcher = Dispatcher(
            self.listener,
            self.pool,
            self.api.handle,
            max_connections=cfg.max_connections,
            idle_timeout=cfg.idle_timeout,
            request_timeout=cfg.request_timeout,
            max_header_bytes=cfg.max_header_bytes,
            max_body_bytes=cfg.max_body_bytes,
            access_log=self.log if cfg.access_log else None,
            log=self.log,
        )
        if self.pg is not None:
            assert self.pg_listener is not None
            self.dispatcher.add_listener(self.pg_listener, self.pg)
        self.started_at = time.monotonic()
        self._thread: threading.Thread | None = None
        self._shutdown_lock = threading.Lock()
        self._shut_down = False
        self._signals = 0

    @property
    def url(self) -> str:
        """HTTP 주소 (HTTP 리스너가 없으면 ValueError)."""
        if self.address is None:
            raise ValueError("HTTP listener is not enabled")
        host, port = self.address
        return f"http://{host}:{port}"

    # ------------------------------------------------------------ 지표
    def stats(self) -> dict[str, Any]:
        ru = resource.getrusage(resource.RUSAGE_SELF)
        cfg = asdict(self.config)
        return {
            "uptime_seconds": time.monotonic() - self.started_at,
            "runtime": runtime_info(),
            "process": {
                "cpu_user_seconds": ru.ru_utime,
                "cpu_system_seconds": ru.ru_stime,
                "max_rss_bytes": ru.ru_maxrss if sys.platform == "darwin" else ru.ru_maxrss * 1024,
                "threads": threading.active_count(),
            },
            "pool": self.pool.metrics(),
            "connections": self.dispatcher.stats(),
            "sessions": self.sessions.stats(),
            "pg": self.pg.stats() if self.pg is not None else None,
            "config": cfg,
        }

    # ------------------------------------------------------------ 시작
    def start(self) -> Server:
        """워커를 띄우고 dispatcher 를 백그라운드 스레드에서 돌린다 (테스트·임베딩용)."""
        self.pool.start()
        self._thread = threading.Thread(target=self.dispatcher.run, name="minidb-dispatcher", daemon=True)
        self._thread.start()
        return self

    def serve_forever(self, *, install_signals: bool = True, ready: Callable[[Server], None] | None = None) -> None:
        """메인 스레드에서 dispatcher 를 돌린다. 신호를 받으면 정상 종료 후 반환한다."""
        if install_signals:
            signal.signal(signal.SIGINT, self._on_signal)
            signal.signal(signal.SIGTERM, self._on_signal)
        self.pool.start()
        if ready is not None:
            ready(self)
        try:
            self.dispatcher.run()
        finally:
            self.shutdown()

    def _on_signal(self, signum: int, _frame: Any) -> None:
        self._signals += 1
        if self._signals >= 2:
            os.write(2, b"minidb: second signal, exiting immediately (no fsync)\n")
            os._exit(130)
        os.write(2, f"minidb: received signal {signum}, shutting down gracefully (send again to force)\n".encode())
        self.dispatcher.stop()

    # ------------------------------------------------------------ 종료
    def shutdown(self, timeout: float | None = 30.0) -> bool:
        """정상 종료. 여러 번 불러도 안전. 처리 중인 요청이 timeout 안에 모두 끝나면 True."""
        with self._shutdown_lock:
            if self._shut_down:
                return True
            self._shut_down = True
        self.dispatcher.stop()
        if self._thread is not None:
            self._thread.join(timeout)
        else:
            self.dispatcher.stopped.wait(timeout)
        clean = self.pool.shutdown(graceful=True, timeout=timeout)
        if not clean:
            self.log("minidb: some requests did not finish before the shutdown timeout")
        try:
            self.db.flush(durable=True)
        finally:
            if self._owns_db:
                self.db.close()
            self.dispatcher.close_wakeup()
        self.log("minidb: server stopped")
        return clean

    def __enter__(self) -> Server:
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()
