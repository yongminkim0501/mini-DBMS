"""RWLock: 다중 읽기, 쓰기 배타, 쓰기 우선, 시간 초과, 중첩 획득 감지."""

import threading
import time

import pytest

from minidb.concurrency import RWLock
from minidb.errors import LockNotAvailable


def run_threads(n, target):
    ts = [threading.Thread(target=target, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    return ts


def join_all(ts, timeout=10):
    for t in ts:
        t.join(timeout)
        assert not t.is_alive(), "thread did not finish (deadlock?)"


def test_multiple_readers_enter_together():
    lock = RWLock("t")
    n = 6
    inside = threading.Barrier(n, timeout=5)  # 모두 동시에 읽기 구간 안에 있어야 통과

    def reader(_i):
        with lock.read_locked():
            inside.wait()

    join_all(run_threads(n, reader))
    assert lock.state() == (0, False, 0)


def test_writer_is_exclusive():
    lock = RWLock("t")
    active = {"readers": 0, "writers": 0, "violations": 0}
    guard = threading.Lock()

    def worker(i):
        for k in range(300):
            if (i + k) % 5 == 0:
                with lock.write_locked():
                    with guard:
                        active["writers"] += 1
                        if active["writers"] != 1 or active["readers"]:
                            active["violations"] += 1
                    time.sleep(0)
                    with guard:
                        active["writers"] -= 1
            else:
                with lock.read_locked():
                    with guard:
                        active["readers"] += 1
                        if active["writers"]:
                            active["violations"] += 1
                    time.sleep(0)
                    with guard:
                        active["readers"] -= 1

    join_all(run_threads(8, worker), timeout=60)
    assert active["violations"] == 0


def test_writer_waits_for_readers_then_blocks_new_readers():
    lock = RWLock("t")
    order = []
    lock.acquire_read()
    w_started = threading.Event()

    def writer():
        w_started.set()
        with lock.write_locked():
            order.append("writer")

    def late_reader():
        with lock.read_locked():
            order.append("late_reader")

    tw = threading.Thread(target=writer)
    tw.start()
    w_started.wait()
    deadline = time.monotonic() + 5
    while lock.state()[2] == 0:  # 쓰기가 줄을 설 때까지
        assert time.monotonic() < deadline
        time.sleep(0.001)
    tr = threading.Thread(target=late_reader)
    tr.start()
    time.sleep(0.05)
    assert order == []  # 쓰기는 기존 읽기를, 새 읽기는 기다리는 쓰기를 기다린다
    lock.release_read()
    join_all([tw, tr])
    assert order == ["writer", "late_reader"]  # 쓰기 우선


def test_writer_not_starved_by_continuous_readers():
    """읽기가 끊임없이 겹쳐 들어와도 쓰기가 제한 시간 안에 락을 얻는다."""
    lock = RWLock("t")
    stop = threading.Event()
    reads = [0] * 6

    def reader(i):
        while not stop.is_set():
            with lock.read_locked():
                reads[i] += 1
                time.sleep(0.0005)  # 읽기 구간이 서로 겹치도록

    ts = run_threads(6, reader)
    time.sleep(0.05)
    acquired = []
    for _ in range(20):
        t0 = time.monotonic()
        with lock.write_locked(timeout=2.0):
            acquired.append(time.monotonic() - t0)
    stop.set()
    join_all(ts)
    assert len(acquired) == 20
    assert max(acquired) < 1.0
    assert sum(reads) > 0


def test_read_timeout_raises_lock_not_available():
    lock = RWLock("users")
    lock.acquire_write()
    result = {}

    def reader():
        t0 = time.monotonic()
        try:
            with lock.read_locked(timeout=0.1):
                result["got"] = True
        except LockNotAvailable as exc:
            result["exc"] = exc
            result["elapsed"] = time.monotonic() - t0

    t = threading.Thread(target=reader)
    t.start()
    t.join(5)
    lock.release_write()
    exc = result["exc"]
    assert exc.sqlstate == "55P03"
    assert exc.message == 'could not obtain lock on relation "users"'
    assert 0.09 <= result["elapsed"] < 2


def test_write_timeout_releases_blocked_readers():
    """시간 초과로 포기한 쓰기가 막고 있던 새 읽기들을 풀어 준다."""
    lock = RWLock("t")
    lock.acquire_read()
    got = threading.Event()

    def writer():
        assert not lock.acquire_write(timeout=0.2)

    def reader():
        with lock.read_locked():
            got.set()

    tw = threading.Thread(target=writer)
    tw.start()
    deadline = time.monotonic() + 5
    while lock.state()[2] == 0:
        assert time.monotonic() < deadline
        time.sleep(0.001)
    tr = threading.Thread(target=reader)
    tr.start()
    tw.join(5)
    assert got.wait(2), "reader stayed blocked after the writer gave up"
    tr.join(5)
    lock.release_read()
    assert lock.state() == (0, False, 0)


def test_timeout_zero_and_success_paths():
    lock = RWLock("t")
    assert lock.acquire_read(timeout=0)
    lock.release_read()
    assert lock.acquire_write(timeout=0)
    lock.release_write()
    with pytest.raises(ValueError):
        lock.acquire_read(timeout=-1)


@pytest.mark.parametrize(
    ("first", "second"),
    [("read", "read"), ("read", "write"), ("write", "read"), ("write", "write")],
)
def test_nested_acquire_detected_in_debug(first, second):
    lock = RWLock("t", debug=True)
    outer = lock.read_locked if first == "read" else lock.write_locked
    inner = lock.read_locked if second == "read" else lock.write_locked
    with outer():
        with pytest.raises(AssertionError, match="while this thread holds"):
            with inner(timeout=0.1):
                pass
    assert lock.state() == (0, False, 0)  # 감지 후에도 상태가 깨지지 않는다


def test_release_errors():
    lock = RWLock("t")
    with pytest.raises(RuntimeError):
        lock.release_read()
    with pytest.raises(RuntimeError):
        lock.release_write()
    lock.acquire_write()
    errors = []

    def other():
        try:
            lock.release_write()
        except RuntimeError as e:
            errors.append(e)

    t = threading.Thread(target=other)
    t.start()
    t.join()
    assert errors  # 다른 스레드가 쓰기 락을 풀 수 없다
    lock.release_write()


def test_exception_inside_block_releases():
    lock = RWLock("t")
    with pytest.raises(ZeroDivisionError):
        with lock.write_locked():
            1 / 0
    with pytest.raises(ZeroDivisionError):
        with lock.read_locked():
            1 / 0
    assert lock.state() == (0, False, 0)
