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

리스닝 소켓을 여러 개 맡을 수 있다: 생성자의 HTTP 리스너(없어도 됨)와 :meth:`Dispatcher.add_listener` 로 붙인
다른 프로토콜(PostgreSQL wire protocol, :mod:`minidb.pgwire.service`)의 리스너. 같은 selector, 같은 워커 풀,
같은 최대 연결 수를 함께 쓴다. HTTP 연결은 읽을 데이터가 생기면 바로 워커에 넘기고(워커가 요청을 읽는다),
다른 프로토콜 연결은 :class:`ConnectionProtocol` 의 ``on_readable`` 이 dispatcher 스레드에서 데이터를 읽어
"완전한 메시지가 있을 때만" 워커에 넘긴다.

한계:
- 최대 연결 수: 넘으면 accept 직후 503 (SQLSTATE 53300) 응답 후 닫는다 (다른 프로토콜은 그 프로토콜의 오류 형식).
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
from typing import Any, Protocol

from .http import MAX_BODY_BYTES, MAX_HEADER_BYTES, HttpError, Request, build_response, parse_request
from .pool import WorkerPool

__all__ = ["Connection", "ConnectionProtocol", "Dispatcher", "Response", "error_body", "DISPATCH", "WAIT"]

#: ConnectionProtocol.on_readable 의 반환값: 완전한 요청이 있으니 워커에 넘긴다 / 더 기다린다
DISPATCH = "dispatch"
WAIT = "wait"

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
    """클라이언트 연결 하나. 한 순간에 한 스레드만 만진다 (모듈 docstring 의 소유권 규칙).

    ``proto`` 가 None 이면 HTTP, 아니면 그 프로토콜 처리기. ``state`` 는 프로토콜별 연결 상태.
    """

    __slots__ = ("sock", "addr", "id", "buf", "last_active", "requests", "proto", "state")
    _ids = itertools.count(1)  # itertools.count 의 next() 는 C 수준에서 원자적이지만, dispatcher 스레드만 부른다

    def __init__(self, sock: socket.socket, addr: tuple, proto: ConnectionProtocol | None = None) -> None:
        self.sock = sock
        self.addr = addr
        self.id = next(Connection._ids)
        self.buf = bytearray()
        self.last_active = time.monotonic()
        self.requests = 0
        self.proto = proto
        self.state: Any = None

    @property
    def peer(self) -> str:
        try:
            host, port = self.addr[0], self.addr[1]
        except (IndexError, TypeError):
            return str(self.addr)
        return f"{host}:{port}"


class ConnectionProtocol(Protocol):
    """HTTP 이외의 프로토콜 처리기가 구현하는 것 (PostgreSQL wire protocol: :class:`minidb.pgwire.service.PgService`).

    dispatcher 스레드에서 불리는 것: ``new_connection``, ``too_many_connections``, ``on_readable``, ``busy``,
    ``idle_limit``, ``goodbye``. 워커에서 불리는 것: ``serve``. ``connection_closed`` 는 연결을 닫는 스레드에서 한 번.
    """

    def new_connection(self, conn: Connection) -> None: ...
    def too_many_connections(self, conn: Connection) -> bytes: ...
    def on_readable(self, conn: Connection) -> str | tuple[str, bytes]: ...
    def serve(self, conn: Connection, dispatcher: Dispatcher) -> None: ...
    def busy(self, conn: Connection) -> tuple[bool, bytes]: ...
    def idle_limit(self, conn: Connection) -> float | None: ...
    def goodbye(self, conn: Connection) -> bytes: ...
    def connection_closed(self, conn: Connection) -> None: ...


@dataclass(frozen=True, slots=True)
class _Listen:
    sock: socket.socket
    proto: ConnectionProtocol | None  # None = HTTP


class Dispatcher:
    """리스닝 소켓(들)을 맡는 디스패처. :meth:`run` 은 호출한 스레드에서 돈다.

    Args:
        listener: bind/listen 이 끝난 HTTP 소켓 (None 이면 HTTP 없이 :meth:`add_listener` 로 붙인 것만).
        pool: 요청을 처리할 워커 풀.
        handler: ``Request -> Response``. 워커 스레드에서 불린다.
        access_log: 요청마다 한 줄 기록 함수 (None 이면 기록 안 함).
    """

    def __init__(
        self,
        listener: socket.socket | None,
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
        self._listeners: list[_Listen] = [_Listen(listener, None)] if listener is not None else []
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

    def add_listener(self, sock: socket.socket, proto: ConnectionProtocol) -> None:
        """다른 프로토콜의 리스닝 소켓을 붙인다 (:meth:`run` 전에)."""
        self._listeners.append(_Listen(sock, proto))

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
        if conn.proto is not None:
            conn.proto.connection_closed(conn)

    def close_connection(self, conn: Connection, final: bytes = b"") -> None:
        """프로토콜 처리기(워커)가 연결을 끝낼 때. ``final`` 이 있으면 보내고 lingering close."""
        if final:
            self._send_and_close(conn, final)
        else:
            self._close(conn)

    def _close_for_shutdown(self, conn: Connection) -> None:
        """서버 종료로 연결을 닫는다. 다른 프로토콜이면 그 프로토콜의 작별 메시지(예: FATAL 57P01)를 먼저 보낸다."""
        if conn.proto is not None:
            try:
                conn.sock.settimeout(0.5)
                conn.sock.sendall(conn.proto.goodbye(conn))
            except OSError:
                pass
        self._close(conn)

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
            self._close_for_shutdown(conn)
            return
        self._returned.put(("idle", conn))
        self._wake()

    # ------------------------------------------------------------ 메인 루프
    def run(self) -> None:
        sel = self._sel
        self._loop_thread = threading.get_ident()
        for lst in self._listeners:
            lst.sock.setblocking(False)
            sel.register(lst.sock, selectors.EVENT_READ, lst)
        sel.register(self._wake_r, selectors.EVENT_READ, "wake")
        tick = max(0.05, min(0.5, self.idle_timeout / 2))
        last_sweep = time.monotonic()
        try:
            while not self.stopping:
                for key, _ in sel.select(timeout=tick):
                    data = key.data
                    if isinstance(data, _Listen):
                        self._accept(data)
                    elif data == "wake":
                        self._drain_wake()
                    elif isinstance(data, tuple):
                        self._linger_read(data[1])
                    else:
                        conn: Connection = data
                        sel.unregister(conn.sock)
                        del self._idle[conn.id]
                        if conn.proto is None:
                            self._dispatch(conn)
                        else:
                            self._proto_readable(conn)
                self._take_returned()
                now = time.monotonic()
                if now - last_sweep >= tick:
                    self._sweep_idle(now)
                    last_sweep = now
        finally:
            self._shutdown_loop()

    def _accept(self, lst: _Listen) -> None:
        while True:
            try:
                sock, addr = lst.sock.accept()
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
            conn = Connection(sock, addr, lst.proto)
            with self._lock:
                self._open += 1
                self._accepted_total += 1
                over = self._open > self.max_connections
                if over:
                    self._rejected_connections += 1
            if lst.proto is not None:
                lst.proto.new_connection(conn)
                if over:
                    self._send_and_close(conn, lst.proto.too_many_connections(conn))
                    continue
            elif over:
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

    def _idle_limit(self, conn: Connection) -> float | None:
        return self.idle_timeout if conn.proto is None else conn.proto.idle_limit(conn)

    def _sweep_idle(self, now: float) -> None:
        expired = []
        for c in self._idle.values():
            limit = self._idle_limit(c)
            if limit is not None and now - c.last_active > limit:
                expired.append(c)
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

    def _proto_readable(self, conn: Connection) -> None:
        """다른 프로토콜 연결에 데이터가 왔다 (dispatcher 스레드): 처리기가 읽고, 완전한 메시지가 있으면 워커로."""
        proto = conn.proto
        assert proto is not None
        try:
            action = proto.on_readable(conn)
        except Exception as exc:  # noqa: BLE001 - 처리기 버그가 dispatcher 를 죽이지 않게
            self.log(f"protocol on_readable failed: {exc!r}")
            self._close(conn)
            return
        if action == WAIT:
            self._register(conn)
            return
        if action != DISPATCH:
            _, final = action
            self.close_connection(conn, final)
            return

        def fail(exc: BaseException) -> None:
            self._close_for_shutdown(conn)

        if self.pool.try_submit(lambda: proto.serve(conn, self), on_error=fail):
            return
        with self._lock:
            self._rejected_busy += 1
        keep, data = proto.busy(conn)  # 큐가 가득 참: 프로토콜 형식의 53000 오류 (연결 유지 여부는 처리기가 정한다)
        if not keep:
            self.close_connection(conn, data)
            return
        try:
            conn.sock.settimeout(1.0)
            conn.sock.sendall(data)
        except OSError:
            self._close(conn)
            return
        self._register(conn)

    def _shutdown_loop(self) -> None:
        """루프가 끝나면: 리스닝 중단, 유휴 연결 닫기. 처리 중인 연결은 워커가 응답 후 닫는다(give_back 이 닫음)."""
        with self._lock:
            self._stopping = True
        for conn in list(self._idle.values()):
            try:
                self._sel.unregister(conn.sock)
            except (KeyError, ValueError):
                pass
            self._close_for_shutdown(conn)
        self._idle.clear()
        for conn, _ in list(self._linger.values()):
            self._linger_drop(conn)
        while True:  # 루프가 끝나기 직전에 반환된 연결
            try:
                self._close_for_shutdown(self._returned.get_nowait()[1])
            except queue.Empty:
                break
        for s in [lst.sock for lst in self._listeners] + [self._wake_r]:
            try:
                self._sel.unregister(s)
            except (KeyError, ValueError):
                pass
        for lst in self._listeners:
            try:
                lst.sock.close()
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
