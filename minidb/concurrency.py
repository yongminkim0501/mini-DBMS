"""동시성 도구: 쓰기 우선 읽기-쓰기 락 (RWLock).

minidb 의 테이블마다 RWLock 이 하나 있다 (docs/server.md "잠금 설계").

- 공유(읽기) 락: 여러 스레드가 동시에 가질 수 있다. SELECT 가 **스냅샷을 잡는 짧은 순간**에만 쓴다.
- 배타(쓰기) 락: 한 스레드만. INSERT 의 파일 append + 인덱스 갱신 전체가 이 구간이다.
- **쓰기 우선**: 쓰기가 기다리고 있으면 새로 오는 읽기는 기다린다. 읽기가 끊임없이 들어와도 쓰기가 굶지 않는다
  (읽기끼리 계속 겹쳐서 읽기 수가 0 이 되는 순간이 영영 오지 않는 문제를 막는다).
- **재진입 미지원**: 같은 스레드가 이미 가진 상태에서 다시 획득하면(읽기→읽기 포함) 쓰기 우선 정책 때문에 교착될 수 있다
  (내가 읽기를 가진 채 기다리는 쓰기 뒤에 줄을 서면, 쓰기는 내 읽기가 풀리기를 기다린다). 디버그 모드(기본: ``__debug__``,
  즉 ``python -O`` 가 아니면 켜짐)에서는 이런 중첩 획득을 즉시 AssertionError 로 잡는다.

구현은 ``threading.Condition`` 하나다. 상태(읽는 스레드 수, 쓰는 스레드, 기다리는 쓰기 수)는 모두 그 조건 변수의 락 아래에서만
읽고 쓴다. GIL 의 암묵적 원자성(``+=`` 등)에 기대지 않으므로 3.14t 에서도 그대로 올바르다.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

from .errors import LockNotAvailable

__all__ = ["RWLock"]


class RWLock:
    """쓰기 우선, 비재진입 읽기-쓰기 락.

    Args:
        name: 오류 메시지에 쓸 이름 (보통 테이블 이름).
        debug: 같은 스레드의 중첩 획득을 AssertionError 로 잡는다 (보유 스레드 추적 비용이 약간 든다).
    """

    __slots__ = ("name", "debug", "_cond", "_readers", "_writer", "_waiting_writers", "_reader_threads")

    def __init__(self, name: str = "", *, debug: bool = __debug__) -> None:
        self.name = name
        self.debug = debug
        self._cond = threading.Condition(threading.Lock())
        # 아래 상태는 모두 self._cond 의 락 아래에서만 접근한다.
        self._readers = 0  # 지금 읽기 락을 가진 스레드 수
        self._writer: int | None = None  # 쓰기 락을 가진 스레드 ident
        self._waiting_writers = 0  # 쓰기 락을 기다리는 스레드 수
        self._reader_threads: dict[int, int] = {}  # debug: 스레드 ident → 보유 수

    # ------------------------------------------------------------ 내부
    def _check_not_held(self, me: int, kind: str) -> None:
        if not self.debug:
            return
        if self._writer == me:
            raise AssertionError(f"RWLock({self.name!r}): {kind} acquire while this thread holds the write lock")
        if me in self._reader_threads:
            raise AssertionError(f"RWLock({self.name!r}): {kind} acquire while this thread holds a read lock")

    @staticmethod
    def _deadline(timeout: float | None) -> float | None:
        if timeout is None:
            return None
        if timeout < 0:
            raise ValueError("timeout must be >= 0")
        return time.monotonic() + timeout

    def _wait(self, deadline: float | None) -> bool:
        """조건 변수에서 한 번 기다린다. 시간이 다 됐으면 False."""
        if deadline is None:
            self._cond.wait()
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        self._cond.wait(remaining)
        return True

    # ------------------------------------------------------------ 읽기
    def acquire_read(self, timeout: float | None = None) -> bool:
        """공유 락을 얻는다. 시간 안에 못 얻으면 False."""
        me = threading.get_ident()
        deadline = self._deadline(timeout)
        with self._cond:
            self._check_not_held(me, "read")
            # 쓰기 우선: 쓰는 중이거나 쓰기가 줄 서 있으면 새 읽기는 기다린다.
            while self._writer is not None or self._waiting_writers:
                if not self._wait(deadline):
                    return False
            self._readers += 1
            if self.debug:
                self._reader_threads[me] = self._reader_threads.get(me, 0) + 1
            return True

    def release_read(self) -> None:
        me = threading.get_ident()
        with self._cond:
            if self._readers <= 0:
                raise RuntimeError(f"RWLock({self.name!r}): release_read without a read lock")
            if self.debug:
                n = self._reader_threads.get(me, 0)
                if n <= 0:
                    raise RuntimeError(f"RWLock({self.name!r}): release_read by a thread that does not hold it")
                if n == 1:
                    del self._reader_threads[me]
                else:
                    self._reader_threads[me] = n - 1
            self._readers -= 1
            if self._readers == 0:
                self._cond.notify_all()  # 기다리는 쓰기를 깨운다

    # ------------------------------------------------------------ 쓰기
    def acquire_write(self, timeout: float | None = None) -> bool:
        """배타 락을 얻는다. 시간 안에 못 얻으면 False."""
        me = threading.get_ident()
        deadline = self._deadline(timeout)
        with self._cond:
            self._check_not_held(me, "write")
            self._waiting_writers += 1  # 이 순간부터 새 읽기는 들어오지 못한다
            try:
                while self._writer is not None or self._readers:
                    if not self._wait(deadline):
                        return False
                self._writer = me
                return True
            finally:
                self._waiting_writers -= 1
                if self._writer != me:
                    # 시간 초과로 포기: 내가 막고 있던 읽기들을 깨운다.
                    self._cond.notify_all()

    def release_write(self) -> None:
        with self._cond:
            if self._writer != threading.get_ident():
                raise RuntimeError(f"RWLock({self.name!r}): release_write by a thread that does not hold it")
            self._writer = None
            self._cond.notify_all()

    # ------------------------------------------------------------ 컨텍스트 매니저
    def _timeout_error(self, kind: str, timeout: float | None) -> LockNotAvailable:
        target = f'relation "{self.name}"' if self.name else "relation"
        return LockNotAvailable(
            f"could not obtain lock on {target}",
            detail=f"Timed out after {timeout} s waiting for a {kind} lock.",
        )

    @contextmanager
    def read_locked(self, timeout: float | None = None) -> Iterator[None]:
        """공유 락 구간. 시간 초과면 LockNotAvailable (55P03)."""
        if not self.acquire_read(timeout):
            raise self._timeout_error("shared", timeout)
        try:
            yield
        finally:
            self.release_read()

    @contextmanager
    def write_locked(self, timeout: float | None = None) -> Iterator[None]:
        """배타 락 구간. 시간 초과면 LockNotAvailable (55P03)."""
        if not self.acquire_write(timeout):
            raise self._timeout_error("exclusive", timeout)
        try:
            yield
        finally:
            self.release_write()

    # ------------------------------------------------------------ 상태 (테스트/진단)
    def state(self) -> tuple[int, bool, int]:
        """(읽기 보유 수, 쓰기 보유 여부, 쓰기 대기 수) 스냅샷."""
        with self._cond:
            return self._readers, self._writer is not None, self._waiting_writers

    def __repr__(self) -> str:
        r, w, ww = self.state()
        return f"RWLock({self.name!r}, readers={r}, writer={w}, waiting_writers={ww})"
