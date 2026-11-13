"""네트워크 디스패처: selectors 로 리스닝 소켓과 **유휴** 연결을 감시하고, 요청이 온 연결만 워커에 넘긴다.

::

      ┌──────────────── dispatcher 스레드 (하나) ────────────────┐
      │ selector: 리스닝 소켓 · 깨우기 소켓 · 유휴 연결들          │
      │   accept ─▶ 등록         읽을 데이터 ─▶ 등록 해제 ─▶ pool.try_submit(serve(conn))
      │   깨우기 ─▶ 반환 큐의 연결 재등록   (가득 차면 503 + Retry-After 후 닫기)
      └───────────────────────────────────────────────────────────┘
                     ▲ SimpleQueue + socketpair 1바이트          │
                     └──────── worker: 요청 읽기 → 처리 → 응답 ◀──┘
                               (keep-alive 이고 버퍼가 비면 반환, 파이프라이닝이면 버퍼의 다음 요청까지 처리)

핵심: 놀고 있는 keep-alive 연결은 selector 에만 있고 워커를 붙잡지 않는다. 워커 수보다 훨씬 많은 연결을 유지할 수 있다.

연결 객체의 소유권은 항상 한 곳에만 있다: (1) dispatcher 의 selector 에 등록됨, (2) 워커가 처리 중, (3) 반환 큐 안.
그래서 연결별 상태(수신 버퍼 등)에는 락이 필요 없다. 넘겨줄 때 큐(내부 락)가 메모리 가시성을 보장한다.
여러 스레드가 함께 쓰는 것은 열린 연결 수와 종료 플래그뿐이고, ``self._lock`` 으로 보호한다.

한계:
- 최대 연결 수: 넘으면 accept 직후 503 (SQLSTATE 53300) 응답 후 닫는다.
- 유휴 연결 타임아웃: dispatcher 가 주기적으로(최대 1초 간격) 오래 논 연결을 닫는다.
- 요청 수신 타임아웃: 워커가 요청 하나를 다 받을 때까지의 시간 제한 (느린 클라이언트 방어). 넘으면 408 후 닫는다.
- 헤더 16 KiB(431), 본문 10 MiB(413) 는 파서가 검사한다.
"""

from __future__ import annotations

import itertools
import json
import queue
import selectors
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .http import MAX_BODY_BYTES, MAX_HEADER_BYTES, HttpError, Request, build_response, parse_request
from .pool import WorkerPool

__all__ = ["Connection", "Dispatcher", "Response", "error_body"]

_RECV = 64 * 1024
_LINGER_SECONDS = 2.0  # 오류 응답 후 상대가 보내던 데이터를 버리며 기다리는 최대 시간


@dataclass(slots=True)
class Response:
    status: int
    body: bytes = b""
    content_type: str | None = "application/json; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)
    close: bool = False  # 처리 결과 연결을 닫아야 하면 True


def error_body(status: int, message: str, sqlstate: str | None = None) -> bytes:
    """프로토콜 수준 오류(JSON 이 아닌 요청, 한계 초과 등)의 본문. /query 의 SQL 오류와 같은 모양의 error 객체."""
    err = {
        "sqlstate": sqlstate,
        "message": message,
        "detail": None,
        "hint": None,
        "position": None,
        "statement_index": None,
        "http_status": status,
    }
    return json.dumps({"results": [], "error": err}, ensure_ascii=False).encode("utf-8")


class Connection:
    """클라이언트 연결 하나. 한 순간에 한 스레드만 만진다 (모듈 docstring 의 소유권 규칙)."""

    __slots__ = ("sock", "addr", "id", "buf", "last_active", "requests")
    _ids = itertools.count(1)  # itertools.count 의 next() 는 C 수준에서 원자적이지만, dispatcher 스레드만 부른다

    def __init__(self, sock: socket.socket, addr: tuple) -> None:
        self.sock = sock
        self.addr = addr
        self.id = next(Connection._ids)
        self.buf = bytearray()
        self.last_active = time.monotonic()
        self.requests = 0

    @property
    def peer(self) -> str:
        try:
            host, port = self.addr[0], self.addr[1]
        except (IndexError, TypeError):
            return str(self.addr)
        return f"{host}:{port}"


class Dispatcher:
    """리스닝 소켓 하나를 맡는 디스패처. :meth:`run` 은 호출한 스레드에서 돈다.

    Args:
        listener: bind/listen 이 끝난 소켓.
        pool: 요청을 처리할 워커 풀.
        handler: ``Request -> Response``. 워커 스레드에서 불린다.
        access_log: 요청마다 한 줄 기록 함수 (None 이면 기록 안 함).
    """

    def __init__(
        self,
        listener: socket.socket,
        pool: WorkerPool,
        handler: Callable[[Request], Response],
        *,
        max_connections: int = 256,
        idle_timeout: float = 60.0,
        request_timeout: float = 10.0,
        max_header_bytes: int = MAX_HEADER_BYTES,
        max_body_bytes: int = MAX_BODY_BYTES,
        retry_after: int = 1,
        access_log: Callable[[str], None] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.listener = listener
        self.pool = pool
        self.handler = handler
        self.max_connections = max_connections
        self.idle_timeout = idle_timeout
        self.request_timeout = request_timeout
        self.max_header_bytes = max_header_bytes
        self.max_body_bytes = max_body_bytes
        self.retry_after = retry_after
        self.access_log = access_log
        self.log = log or (lambda m: None)

        self._sel = selectors.DefaultSelector()
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._wake_w.setblocking(False)
        self._returned: queue.SimpleQueue[tuple[str, Connection]] = queue.SimpleQueue()
        self._idle: dict[int, Connection] = {}  # dispatcher 스레드 전용
        self._linger: dict[int, tuple[Connection, float]] = {}  # dispatcher 스레드 전용
        self._loop_thread: int | None = None

        self._lock = threading.Lock()
        # 아래는 self._lock 아래에서만 접근 (워커와 dispatcher 가 함께 씀)
        self._open = 0
        self._stopping = False
        self._accepted_total = 0
        self._rejected_connections = 0
        self._rejected_busy = 0
        self._timeouts = 0
        self.stopped = threading.Event()

    # ------------------------------------------------------------ 상태
    @property
    def stopping(self) -> bool:
        with self._lock:
            return self._stopping

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "open_connections": self._open,
                "max_connections": self.max_connections,
                "accepted_total": self._accepted_total,
                "rejected_max_connections": self._rejected_connections,
                "rejected_busy": self._rejected_busy,
                "request_timeouts": self._timeouts,
            }

    def stop(self) -> None:
        """새 연결 수락을 멈추고 유휴 연결을 닫도록 dispatcher 를 깨운다 (어느 스레드에서나, 시그널 처리기에서도)."""
        with self._lock:
            self._stopping = True
        self._wake()

    def _wake(self) -> None:
        try:
            self._wake_w.send(b"\x00")
        except (BlockingIOError, OSError):
            pass  # 버퍼가 차 있으면 이미 깨어날 예정이다

    # ------------------------------------------------------------ 연결 수명
    def _close(self, conn: Connection) -> None:
        try:
            conn.sock.close()
        except OSError:
            pass
        with self._lock:
            self._open -= 1

    def _send_and_close(self, conn: Connection, resp: bytes) -> None:
        """오류 응답을 보내고 닫는다 (lingering close).

        상대가 보낸 요청 바이트를 읽지 않은 채 바로 close 하면 OS 가 RST 를 보내고, 상대는 우리가 보낸 응답을 읽기 전에
        "connection reset" 을 받을 수 있다. 그래서 응답 후 쓰기 방향만 닫고(FIN), 남은 입력을 버리며 상대가 닫거나
        잠시(_LINGER_SECONDS) 지날 때까지 기다린 다음 닫는다. 기다리는 일은 dispatcher 의 selector 가 한다(워커를 붙잡지 않음).
        """
        try:
            conn.sock.settimeout(1.0)  # 작은 응답이라 소켓 버퍼에 바로 들어간다. 상대가 안 읽어도 오래 막히지 않게
            conn.sock.sendall(resp)
            conn.sock.shutdown(socket.SHUT_WR)
        except OSError:
            self._close(conn)
            return
        if threading.get_ident() == self._loop_thread:
            self._linger_add(conn)
        elif self.stopped.is_set():
            self._close(conn)
        else:
            self._returned.put(("linger", conn))
            self._wake()

    def _linger_add(self, conn: Connection) -> None:
        conn.sock.setblocking(False)
        self._linger[conn.id] = (conn, time.monotonic() + _LINGER_SECONDS)
        self._sel.register(conn.sock, selectors.EVENT_READ, ("linger", conn))

    def _linger_drop(self, conn: Connection) -> None:
        self._linger.pop(conn.id, None)
        try:
            self._sel.unregister(conn.sock)
        except (KeyError, ValueError):
            pass
        self._close(conn)

    def _linger_read(self, conn: Connection) -> None:
        try:
            data = conn.sock.recv(_RECV)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self._linger_drop(conn)

    def _register(self, conn: Connection) -> None:
        conn.last_active = time.monotonic()
        self._idle[conn.id] = conn
        self._sel.register(conn.sock, selectors.EVENT_READ, conn)

    def give_back(self, conn: Connection) -> None:
        """워커가 keep-alive 연결을 dispatcher 에 돌려준다 (워커 스레드에서 호출)."""
        if self.stopping:
            self._close(conn)
            return
        self._returned.put(("idle", conn))
        self._wake()

    # ------------------------------------------------------------ 메인 루프
    def run(self) -> None:
        sel = self._sel
        self._loop_thread = threading.get_ident()
        self.listener.setblocking(False)
        sel.register(self.listener, selectors.EVENT_READ, "listen")
        sel.register(self._wake_r, selectors.EVENT_READ, "wake")
        tick = max(0.05, min(0.5, self.idle_timeout / 2))
        last_sweep = time.monotonic()
        try:
            while not self.stopping:
                for key, _ in sel.select(timeout=tick):
                    data = key.data
                    if data == "listen":
                        self._accept()
                    elif data == "wake":
                        self._drain_wake()
                    elif isinstance(data, tuple):
                        self._linger_read(data[1])
                    else:
                        conn: Connection = data
                        sel.unregister(conn.sock)
                        del self._idle[conn.id]
                        self._dispatch(conn)
                self._take_returned()
                now = time.monotonic()
                if now - last_sweep >= tick:
                    self._sweep_idle(now)
                    last_sweep = now
        finally:
            self._shutdown_loop()

    def _accept(self) -> None:
        while True:
            try:
                sock, addr = self.listener.accept()
            except (BlockingIOError, InterruptedError):
                return
            except OSError as exc:
                self.log(f"accept failed: {exc}")
                return
            sock.setblocking(True)
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            conn = Connection(sock, addr)
            with self._lock:
                self._open += 1
                self._accepted_total += 1
                over = self._open > self.max_connections
                if over:
                    self._rejected_connections += 1
            if over:
                body = error_body(503, "sorry, too many clients already", "53300")
                self._send_and_close(conn, build_response(503, body, keep_alive=False))
                continue
            self._register(conn)

    def _drain_wake(self) -> None:
        try:
            while self._wake_r.recv(4096):
                pass
        except (BlockingIOError, OSError):
            pass

    def _take_returned(self) -> None:
        while True:
            try:
                kind, conn = self._returned.get_nowait()
            except queue.Empty:
                return
            if kind == "linger":
                self._linger_add(conn)
            else:
                self._register(conn)

    def _sweep_idle(self, now: float) -> None:
        expired = [c for c in self._idle.values() if now - c.last_active > self.idle_timeout]
        for conn in expired:
            self._sel.unregister(conn.sock)
            del self._idle[conn.id]
            self._close(conn)
        for conn, deadline in list(self._linger.values()):
            if now >= deadline:
                self._linger_drop(conn)

    def _dispatch(self, conn: Connection) -> None:
        def fail(exc: BaseException) -> None:
            # serve 자체가 예상 못한 예외로 끝났거나(버그) 종료로 취소된 경우: 500/503 후 닫기
            status = 503 if type(exc).__name__ == "QueryCanceled" else 500
            body = error_body(status, str(exc) or type(exc).__name__, "57014" if status == 503 else "XX000")
            self._send_and_close(conn, build_response(status, body, keep_alive=False))

        if not self.pool.try_submit(lambda: self.serve(conn), on_error=fail):
            with self._lock:
                self._rejected_busy += 1
            body = error_body(503, "server is busy: worker queue is full, retry later", "53000")
            resp = build_response(503, body, keep_alive=False, headers={"Retry-After": str(self.retry_after)})
            self._send_and_close(conn, resp)

    def _shutdown_loop(self) -> None:
        """루프가 끝나면: 리스닝 중단, 유휴 연결 닫기. 처리 중인 연결은 워커가 응답 후 닫는다(give_back 이 닫음)."""
        with self._lock:
            self._stopping = True
        for conn in list(self._idle.values()):
            try:
                self._sel.unregister(conn.sock)
            except (KeyError, ValueError):
                pass
            self._close(conn)
        self._idle.clear()
        for conn, _ in list(self._linger.values()):
            self._linger_drop(conn)
        while True:  # 루프가 끝나기 직전에 반환된 연결
            try:
                self._close(self._returned.get_nowait()[1])
            except queue.Empty:
                break
        for s in (self.listener, self._wake_r):
            try:
                self._sel.unregister(s)
            except (KeyError, ValueError):
                pass
        try:
            self.listener.close()
        except OSError:
            pass
        self._sel.close()
        self.stopped.set()

    def close_wakeup(self) -> None:
        for s in (self._wake_r, self._wake_w):
            try:
                s.close()
            except OSError:
                pass

    # ------------------------------------------------------------ 워커 쪽: 요청 처리
    def serve(self, conn: Connection) -> None:
        """워커 스레드에서: 버퍼의 요청을 차례로 처리하고, keep-alive 면 연결을 dispatcher 에 돌려준다."""
        sock = conn.sock
        served = 0
        deadline: float | None = None
        while True:
            try:
                parsed = parse_request(
                    conn.buf, max_header_bytes=self.max_header_bytes, max_body_bytes=self.max_body_bytes
                )
            except HttpError as exc:
                self._respond_error(conn, exc.status, exc.message)
                return
            if parsed is None:
                if not conn.buf and served:
                    self.give_back(conn)  # 버퍼가 비었다: 다음 요청은 dispatcher 가 기다린다
                    return
                now = time.monotonic()
                if deadline is None:
                    deadline = now + self.request_timeout
                remaining = deadline - now
                if remaining <= 0:
                    self._request_timeout(conn)
                    return
                try:
                    sock.settimeout(remaining)
                    data = sock.recv(_RECV)
                except TimeoutError:
                    self._request_timeout(conn)
                    return
                except OSError:
                    self._close(conn)
                    return
                if not data:  # 상대가 닫았다
                    self._close(conn)
                    return
                conn.buf += data
                continue

            req, used = parsed
            del conn.buf[:used]
            deadline = None
            t0 = time.perf_counter()
            try:
                resp = self.handler(req)
            except Exception as exc:  # noqa: BLE001 - 처리기 버그도 500 으로 격리하고 워커는 계속
                self.log(f"handler failed for {req.method} {req.path}: {exc!r}")
                resp = Response(500, error_body(500, f"internal error: {type(exc).__name__}", "XX000"), close=True)
            keep = req.keep_alive and not resp.close and not self.stopping
            out = build_response(
                resp.status,
                resp.body,
                content_type=resp.content_type,
                keep_alive=keep,
                headers=resp.headers,
                head_only=req.method == "HEAD",
            )
            try:
                sock.settimeout(self.request_timeout)
                sock.sendall(out)
            except OSError:
                self._close(conn)
                return
            conn.requests += 1
            served += 1
            if self.access_log is not None:
                ms = (time.perf_counter() - t0) * 1000
                self.access_log(
                    f"{_now_iso()} {conn.peer} {req.method} {req.path} {resp.status} {ms:.3f}ms "
                    f"{threading.current_thread().name}"
                )
            if not keep:
                self._close(conn)
                return

    def _respond_error(self, conn: Connection, status: int, message: str) -> None:
        body = error_body(status, message, "08P01")  # protocol_violation
        self._send_and_close(conn, build_response(status, body, keep_alive=False))
        if self.access_log is not None:
            self.access_log(f"{_now_iso()} {conn.peer} - - {status} 0.000ms {threading.current_thread().name}")

    def _request_timeout(self, conn: Connection) -> None:
        with self._lock:
            self._timeouts += 1
        if conn.buf:  # 요청을 받던 중이면 408, 아무것도 안 왔으면 조용히 닫는다
            self._respond_error(conn, 408, f"request not received within {self.request_timeout} s")
        else:
            self._close(conn)


def _now_iso() -> str:
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) + f".{int(t % 1 * 1000):03d}"
