"""PostgreSQL wire protocol 리스너를 Phase 3 서버(같은 엔진, 같은 워커 풀, 같은 디스패처)에 붙이는 처리기.

역할 분담::

    dispatcher 스레드 (on_readable)                     워커 (serve)
    ─────────────────────────────────────              ─────────────────────────────────────
    recv → conn.buf 에 붙이기                            버퍼의 완전한 메시지를 차례로 PgBackend 에 넘김
    시작 단계: SSLRequest/GSSENCRequest → 'N'           (확장 쿼리는 보통 P/B/D/E/S 가 한 번에 온다)
              CancelRequest → 여기서 바로 처리 후 닫기    응답은 Output 으로 스트리밍 (64 KiB 마다 전송)
    완전한 메시지가 있으면 → 워커에 넘김                    더 처리할 것이 없으면 → 연결을 dispatcher 에 반환
    없으면 → 다시 selector 에 등록 (워커를 붙잡지 않음)

**취소를 디스패처에서 처리하는 이유**: 취소는 별도 연결로 온다. 그 연결을 워커에 넘기면, 모든 워커가 오래 걸리는
쿼리를 실행 중일 때 취소 요청 자체가 큐에서 기다리게 되어 정작 취소가 필요한 순간에 동작하지 않는다.
취소 처리는 (연결 번호, 비밀 키) 확인과 플래그 하나를 켜는 일이라 dispatcher 스레드에서 해도 짧다.

공유 상태(연결 번호 → PgBackend, 지표)는 ``self._lock`` 아래에서만 바꾼다.
"""

from __future__ import annotations

import secrets
import struct
import threading
import time
from collections.abc import Callable
from typing import Any

from ..engine import Database
from ..errors import AdminShutdown, DatabaseError, InsufficientResources, ProtocolViolation, TooManyConnections
from ..server.dispatcher import DISPATCH, WAIT, Connection, Dispatcher
from . import messages as m
from .protocol import Output, PgBackend

__all__ = ["PgService", "DEFAULT_PG_DATABASE"]

DEFAULT_PG_DATABASE = "minidb"
_RECV = 64 * 1024
_CANCEL = struct.Struct("!II")


def _now_iso() -> str:
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) + f".{int(t % 1 * 1000):03d}"


class PgService:
    """PG 프로토콜 연결 처리기 (:class:`~minidb.server.dispatcher.ConnectionProtocol` 구현).

    Args:
        db: 공유 Database.
        database_name: 접속을 허용하는 데이터베이스 이름 (``--pg-database``).
        log: 오류/접속 로그 함수.
        access_log: 접속/종료 로그를 남길지.
        startup_timeout: 시작 패킷을 다 받기까지의 제한 (PostgreSQL authentication_timeout 과 같은 60초).
        idle_timeout: 시작 이후 유휴 연결 제한 (None 이면 없음, PostgreSQL 기본과 같다).
        send_timeout: 응답 전송 제한 (클라이언트가 읽지 않으면 워커가 무한히 막히지 않게).
        max_message_bytes: 메시지 최대 길이 (기본 1GB 미만).
    """

    def __init__(
        self,
        db: Database,
        *,
        database_name: str = DEFAULT_PG_DATABASE,
        log: Callable[[str], None] | None = None,
        access_log: bool = True,
        startup_timeout: float = 60.0,
        idle_timeout: float | None = None,
        send_timeout: float = 60.0,
        max_message_bytes: int = m.MAX_MESSAGE_BYTES,
    ) -> None:
        self.db = db
        self.database_name = database_name
        self._log = log or (lambda line: None)
        self.access_log = access_log
        self.startup_timeout = startup_timeout
        self.idle_timeout = idle_timeout
        self.send_timeout = send_timeout
        self.max_message_bytes = max_message_bytes
        self._lock = threading.Lock()
        # 아래는 self._lock 아래에서만 접근
        self._backends: dict[int, PgBackend] = {}  # 시작이 끝난 연결 (취소 요청 대상)
        self._open = 0
        self._running = 0
        self._sessions_total = 0
        self._cancel_requests = 0
        self._cancels_delivered = 0
        self._rejected_busy = 0

    # ------------------------------------------------------------ BackendHost (PgBackend 가 부른다)
    def log(self, line: str) -> None:
        self._log(line)

    def register_backend(self, backend: PgBackend) -> None:
        with self._lock:
            self._backends[backend.id] = backend
            self._sessions_total += 1
        if self.access_log:
            self._log(
                f"{_now_iso()} pg connection authorized: id={backend.id} user={backend.user} "
                f"database={backend.database} application_name={backend.application_name!r} remote={backend.peer}"
            )

    def query_started(self) -> None:
        with self._lock:
            self._running += 1

    def query_finished(self) -> None:
        with self._lock:
            self._running -= 1

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "database": self.database_name,
                "connections": self._open,
                "sessions": len(self._backends),
                "running_queries": self._running,
                "sessions_total": self._sessions_total,
                "cancel_requests": self._cancel_requests,
                "cancels_delivered": self._cancels_delivered,
                "rejected_busy": self._rejected_busy,
            }

    # ------------------------------------------------------------ ConnectionProtocol (dispatcher 가 부른다)
    def new_connection(self, conn: Connection) -> None:
        conn.state = PgBackend(self, conn.id, conn.peer)
        with self._lock:
            self._open += 1

    def too_many_connections(self, conn: Connection) -> bytes:
        return m.error_response(TooManyConnections("sorry, too many clients already", severity="FATAL"))

    def idle_limit(self, conn: Connection) -> float | None:
        return self.idle_timeout if conn.state.started else self.startup_timeout

    def goodbye(self, conn: Connection) -> bytes:
        if not conn.state.started:
            return b""
        return m.error_response(AdminShutdown("terminating connection due to administrator command"))

    def connection_closed(self, conn: Connection) -> None:
        b: PgBackend = conn.state
        with self._lock:
            self._open -= 1
            self._backends.pop(b.id, None)
        b.close()
        if self.access_log and b.started:
            dur = time.monotonic() - b.connected_at
            self._log(
                f"{_now_iso()} pg disconnection: id={b.id} user={b.user} database={b.database} "
                f"remote={b.peer} session_time={dur:.3f}s queries={b.queries}"
            )

    def on_readable(self, conn: Connection) -> str | tuple[str, bytes]:
        """dispatcher 스레드: 읽고, 시작 단계의 SSL/GSS/취소 요청은 직접 처리하고, 완전한 메시지가 있으면 DISPATCH."""
        try:
            data = conn.sock.recv(_RECV)
        except (BlockingIOError, InterruptedError):
            return WAIT
        except OSError:
            return ("close", b"")
        if not data:
            return ("close", b"")
        conn.buf += data
        b: PgBackend = conn.state
        try:
            if b.started:
                return DISPATCH if m.has_complete_message(conn.buf, max_length=self.max_message_bytes) else WAIT
            while True:
                code = m.peek_startup_packet(conn.buf)
                if code is None:
                    return WAIT
                if code in (m.SSL_REQUEST_CODE, m.GSSENC_REQUEST_CODE):
                    m.take_startup_packet(conn.buf)
                    conn.sock.settimeout(1.0)
                    conn.sock.sendall(b"N")  # 암호화 미지원: 평문으로 계속 (다음 시작 패킷을 기다린다)
                    continue
                if code == m.CANCEL_REQUEST_CODE:
                    pkt = m.take_startup_packet(conn.buf)
                    assert pkt is not None
                    self.cancel(pkt.payload)
                    return ("close", b"")  # 취소 연결에는 아무 응답도 보내지 않는다 (PostgreSQL 과 같다)
                return DISPATCH
        except ProtocolViolation as exc:
            return ("close", m.error_response(exc))
        except OSError:
            return ("close", b"")

    def cancel(self, payload: bytes) -> bool:
        """CancelRequest 의 (연결 번호, 비밀 키)가 맞으면 그 연결의 실행 중인 문장에 취소 플래그를 켠다. 틀리면 조용히 무시."""
        with self._lock:
            self._cancel_requests += 1
        if len(payload) != _CANCEL.size:
            return False
        pid, key = _CANCEL.unpack(payload)
        with self._lock:
            target = self._backends.get(pid)
        if target is None or not secrets.compare_digest(target.secret.to_bytes(4, "big"), key.to_bytes(4, "big")):
            return False
        delivered = target.request_cancel()
        if delivered:
            with self._lock:
                self._cancels_delivered += 1
        if self.access_log:
            self._log(f"{_now_iso()} pg cancel request: id={pid} delivered={delivered}")
        return delivered

    def busy(self, conn: Connection) -> tuple[bool, bytes]:
        """워커 큐가 가득 찼을 때 (dispatcher 스레드). 시작 단계면 FATAL 53300 후 종료.

        시작 이후에는 버퍼의 메시지를 실행하지 않고 프로토콜 규칙대로 53000 오류로 답한다: 단순 쿼리는
        ErrorResponse + ReadyForQuery, 확장 쿼리는 첫 메시지에 ErrorResponse 를 보내고 Sync 까지 버린 뒤 ReadyForQuery.
        연결은 유지한다 (클라이언트가 다시 시도할 수 있다).
        """
        with self._lock:
            self._rejected_busy += 1
        b: PgBackend = conn.state
        if not b.started:
            return False, m.error_response(TooManyConnections("sorry, too many clients already", severity="FATAL"))
        err = m.error_response(InsufficientResources("server is busy: worker queue is full, retry later"))
        out = bytearray()
        try:
            while (msg := m.take_message(conn.buf, max_length=self.max_message_bytes)) is not None:
                kind = msg[0]
                if kind == 0x58:  # Terminate
                    return False, bytes(out)
                if kind == 0x53:  # Sync
                    b.skip_until_sync = False
                    out += m.ready_for_query()
                elif b.skip_until_sync or kind == 0x48:
                    continue
                elif kind == 0x51:
                    out += err + m.ready_for_query()
                else:
                    out += err
                    b.skip_until_sync = True
        except ProtocolViolation as exc:
            return False, bytes(out) + m.error_response(exc)
        return True, bytes(out)

    def serve(self, conn: Connection, dispatcher: Dispatcher) -> None:
        """워커: 버퍼의 완전한 메시지를 모두 처리하고 연결을 돌려주거나 닫는다."""
        b: PgBackend = conn.state
        sock = conn.sock

        def send(data: bytes) -> None:
            sock.settimeout(self.send_timeout)
            sock.sendall(data)

        out = Output(send)
        keep = True
        try:
            while keep:
                if not b.started:
                    pkt = m.take_startup_packet(conn.buf)
                    if pkt is None:
                        break
                    if pkt.code in (m.SSL_REQUEST_CODE, m.GSSENC_REQUEST_CODE):
                        out.write(b"N")
                        continue
                    if pkt.code == m.CANCEL_REQUEST_CODE:
                        self.cancel(pkt.payload)
                        keep = False
                        break
                    keep = b.handle_startup(m.parse_startup_params(pkt.payload), pkt.code, out)
                    continue
                msg = m.take_message(conn.buf, max_length=self.max_message_bytes)
                if msg is None:
                    break
                conn.requests += 1
                keep = b.handle_message(msg[0], msg[1], out)
        except ProtocolViolation as exc:
            out.write(m.error_response(exc))
            keep = False
        except DatabaseError as exc:  # 시작 패킷 해석 오류 등
            out.write(m.error_response(exc))
            keep = False
        except OSError:
            dispatcher.close_connection(conn)
            return
        if keep:
            try:
                out.flush()
            except OSError:
                dispatcher.close_connection(conn)
                return
            dispatcher.give_back(conn)
        else:
            dispatcher.close_connection(conn, out.take())
