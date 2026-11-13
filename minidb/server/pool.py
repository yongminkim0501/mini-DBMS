"""고정 크기 스레드 풀 (ThreadPoolExecutor 를 쓰지 않고 직접 구현).

::

    try_submit(task) ──▶ queue.Queue(maxsize) ──▶ worker-1 … worker-N  (각자 get → 실행 → 다음 get)
          │ 큐가 차 있으면 즉시 False (호출자가 503 + Retry-After 로 응답: 백프레셔)

- 작업 예외는 워커 안에서 격리한다: 기록하고 작업의 ``on_error(exc)`` 를 불러(HTTP 요청이면 500 응답) 워커는 계속 돈다.
- ``shutdown(graceful=True)``: 새 작업 거부 → 큐에 이미 있는 작업은 모두 처리 → 워커마다 종료 표식(sentinel)을 넣어
  FIFO 순서상 남은 작업 **뒤에서** 끝나게 한 다음 join. ``graceful=False`` 면 큐의 작업을 버리고(각 작업에
  ``on_error(QueryCanceled)``) 진행 중인 작업만 끝나기를 기다린다.
- 지표는 모두 ``self._lock`` 아래에서 갱신·조회한다 (3.14t 에서 ``+=`` 는 원자적이지 않다).
"""

from __future__ import annotations

import queue
import sys
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..errors import QueryCanceled

__all__ = ["WorkerPool", "LATENCY_BUCKETS_MS"]

#: 처리 시간 히스토그램의 버킷 상한 (ms). 마지막 버킷은 그 이상 전부.
LATENCY_BUCKETS_MS: tuple[float, ...] = (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000)

_STOP = object()  # 워커 종료 표식


@dataclass(slots=True)
class _Task:
    fn: Callable[[], Any]
    on_error: Callable[[BaseException], Any] | None


def _default_log(msg: str) -> None:
    sys.stderr.write(msg + "\n")


class WorkerPool:
    """이름 있는 워커 스레드 N 개와 크기 제한 큐.

    Args:
        num_workers: 워커 스레드 수 (>= 1).
        queue_size: 대기 큐 최대 길이 (>= 1). 차면 :meth:`try_submit` 이 False.
        name: 스레드 이름 접두사 (``minidb-worker-3``).
        log: 오류 기록 함수 (기본 stderr).
    """

    def __init__(
        self,
        num_workers: int,
        queue_size: int,
        *,
        name: str = "minidb-worker",
        log: Callable[[str], None] | None = None,
    ) -> None:
        if num_workers < 1:
            raise ValueError("num_workers must be >= 1")
        if queue_size < 1:
            raise ValueError("queue_size must be >= 1")
        self.num_workers = num_workers
        self.queue_size = queue_size
        self.name = name
        self._log = log or _default_log
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=queue_size)
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        # 아래는 모두 self._lock 아래에서만 접근
        self._accepting = False
        self._active = 0
        self._completed = 0
        self._failed = 0
        self._rejected = 0
        self._canceled = 0
        self._hist = [0] * (len(LATENCY_BUCKETS_MS) + 1)
        self._busy_seconds = 0.0

    # ------------------------------------------------------------ 수명
    def start(self) -> WorkerPool:
        with self._lock:
            if self._threads:
                raise RuntimeError("pool already started")
            self._accepting = True
            for i in range(1, self.num_workers + 1):
                t = threading.Thread(target=self._worker, name=f"{self.name}-{i}", daemon=True)
                self._threads.append(t)
        for t in self._threads:
            t.start()
        return self

    def try_submit(self, fn: Callable[[], Any], on_error: Callable[[BaseException], Any] | None = None) -> bool:
        """작업을 큐에 넣는다. 큐가 차 있거나 종료 중이면 즉시 False (기다리지 않는다)."""
        with self._lock:
            if not self._accepting:
                self._rejected += 1
                return False
            try:
                # 락 안에서 넣어야 shutdown 의 "거부 시작"과 순서가 꼬이지 않는다 (종료 표식 뒤에 작업이 들어가지 않게).
                self._queue.put_nowait(_Task(fn, on_error))
            except queue.Full:
                self._rejected += 1
                return False
            return True

    def shutdown(self, graceful: bool = True, timeout: float | None = None) -> bool:
        """새 작업을 거부하고 워커를 끝낸다. 모든 워커가 timeout 안에 끝나면 True."""
        with self._lock:
            was_accepting = self._accepting
            self._accepting = False
        if not graceful:
            self._drain_cancel()
        if was_accepting:
            deadline = None if timeout is None else time.monotonic() + timeout
            for _ in self._threads:
                # 큐가 차 있으면 자리가 날 때까지 기다린다 (워커들이 계속 비우고 있다).
                while True:
                    try:
                        self._queue.put(_STOP, timeout=0.05)
                        break
                    except queue.Full:
                        if deadline is not None and time.monotonic() > deadline:
                            return False
        deadline = None if timeout is None else time.monotonic() + timeout
        for t in self._threads:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            t.join(remaining)
        return not any(t.is_alive() for t in self._threads)

    def _drain_cancel(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is _STOP:
                self._queue.put_nowait(item)
                return
            with self._lock:
                self._canceled += 1
            self._call_on_error(item, QueryCanceled("canceling statement due to server shutdown"))

    # ------------------------------------------------------------ 워커
    def _call_on_error(self, task: _Task, exc: BaseException) -> None:
        if task.on_error is None:
            return
        try:
            task.on_error(exc)
        except BaseException:  # noqa: BLE001 - 오류 처리기의 오류도 워커를 죽이면 안 된다
            self._log(f"{threading.current_thread().name}: error handler failed\n{traceback.format_exc()}")

    def _worker(self) -> None:
        q = self._queue
        clock = time.perf_counter
        while True:
            item = q.get()
            if item is _STOP:
                return
            with self._lock:
                self._active += 1
            t0 = clock()
            ok = True
            try:
                item.fn()
            except BaseException as exc:  # noqa: BLE001 - 격리: 어떤 예외도 워커를 죽이지 않는다
                ok = False
                self._log(f"{threading.current_thread().name}: task failed\n{traceback.format_exc()}")
                self._call_on_error(item, exc)
            elapsed = clock() - t0
            ms = elapsed * 1000
            b = 0
            while b < len(LATENCY_BUCKETS_MS) and ms > LATENCY_BUCKETS_MS[b]:
                b += 1
            with self._lock:
                self._active -= 1
                self._busy_seconds += elapsed
                self._hist[b] += 1
                if ok:
                    self._completed += 1
                else:
                    self._failed += 1

    # ------------------------------------------------------------ 지표
    def metrics(self) -> dict[str, Any]:
        """스레드 안전한 지표 스냅샷."""
        with self._lock:
            hist = {f"le_{int(b)}ms": n for b, n in zip(LATENCY_BUCKETS_MS, self._hist)}
            hist[f"gt_{int(LATENCY_BUCKETS_MS[-1])}ms"] = self._hist[-1]
            return {
                "workers": self.num_workers,
                "active_workers": self._active,
                "queue_length": self._queue.qsize(),
                "queue_size": self.queue_size,
                "completed": self._completed,
                "failed": self._failed,
                "rejected": self._rejected,
                "canceled": self._canceled,
                "busy_seconds": self._busy_seconds,
                "latency_histogram": hist,
                "accepting": self._accepting,
            }

    @property
    def thread_names(self) -> list[str]:
        return [t.name for t in self._threads]
