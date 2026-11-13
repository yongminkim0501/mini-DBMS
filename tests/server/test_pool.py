"""WorkerPool: 큐 포화 거부, 예외 격리, graceful shutdown, 지표."""

import threading
import time

import pytest

from minidb.errors import QueryCanceled
from minidb.server.pool import WorkerPool


@pytest.fixture
def logs():
    return []


def test_runs_tasks_on_named_workers(logs):
    pool = WorkerPool(3, 10, name="w", log=logs.append).start()
    names = set()
    done = threading.Event()
    lock = threading.Lock()
    count = [0]

    def task():
        with lock:
            names.add(threading.current_thread().name)
            count[0] += 1
            if count[0] == 30:
                done.set()
        time.sleep(0.002)

    for _ in range(30):
        while not pool.try_submit(task):
            time.sleep(0.001)
    assert done.wait(5)
    assert pool.shutdown(timeout=5)
    assert names <= {"w-1", "w-2", "w-3"} and len(names) >= 2
    assert pool.metrics()["completed"] == 30


def test_rejects_when_queue_full(logs):
    pool = WorkerPool(1, 2, log=logs.append).start()
    gate = threading.Event()
    started = threading.Event()

    def blocker():
        started.set()
        gate.wait(5)

    assert pool.try_submit(blocker)
    assert started.wait(5)  # 워커 하나가 막혀 있다
    assert pool.try_submit(lambda: None)
    assert pool.try_submit(lambda: None)  # 큐 2칸이 찼다
    t0 = time.monotonic()
    assert not pool.try_submit(lambda: None)  # 즉시 거부 (기다리지 않음)
    assert time.monotonic() - t0 < 0.1
    m = pool.metrics()
    assert m["rejected"] == 1 and m["queue_length"] == 2 and m["active_workers"] == 1
    gate.set()
    assert pool.shutdown(timeout=5)
    assert pool.metrics()["completed"] == 3


def test_exception_isolated_and_on_error_called(logs):
    pool = WorkerPool(1, 10, log=logs.append).start()
    seen = []
    ok = threading.Event()

    def boom():
        raise ValueError("bad task")

    assert pool.try_submit(boom, on_error=seen.append)
    assert pool.try_submit(ok.set)  # 같은 (유일한) 워커가 계속 일한다
    assert ok.wait(5)
    assert pool.shutdown(timeout=5)
    assert len(seen) == 1 and isinstance(seen[0], ValueError)
    assert any("bad task" in line for line in logs)
    m = pool.metrics()
    assert m["failed"] == 1 and m["completed"] == 1


def test_failing_error_handler_does_not_kill_worker(logs):
    pool = WorkerPool(1, 10, log=logs.append).start()
    ok = threading.Event()

    def handler(_e):
        raise RuntimeError("handler broke")

    pool.try_submit(lambda: 1 / 0, on_error=handler)
    pool.try_submit(ok.set)
    assert ok.wait(5)
    assert pool.shutdown(timeout=5)
    assert any("handler broke" in line for line in logs)


def test_graceful_shutdown_drains_queue(logs):
    pool = WorkerPool(2, 100, log=logs.append).start()
    done = []
    lock = threading.Lock()

    def task(i):
        time.sleep(0.005)
        with lock:
            done.append(i)

    for i in range(60):
        assert pool.try_submit(lambda i=i: task(i))
    assert pool.shutdown(graceful=True, timeout=10)
    assert sorted(done) == list(range(60))  # 큐에 있던 작업을 모두 처리
    assert not pool.try_submit(lambda: None)  # 종료 후에는 거부
    assert all(not t.is_alive() for t in pool._threads)


def test_non_graceful_shutdown_cancels_queued(logs):
    pool = WorkerPool(1, 100, log=logs.append).start()
    gate = threading.Event()
    started = threading.Event()
    canceled = []

    def blocker():
        started.set()
        gate.wait(5)

    pool.try_submit(blocker)
    assert started.wait(5)
    for _ in range(5):
        pool.try_submit(lambda: None, on_error=canceled.append)
    threading.Timer(0.1, gate.set).start()
    assert pool.shutdown(graceful=False, timeout=5)
    assert len(canceled) == 5 and all(isinstance(e, QueryCanceled) for e in canceled)
    assert pool.metrics()["canceled"] == 5


def test_metrics_histogram_and_thread_safety(logs):
    pool = WorkerPool(4, 1000, log=logs.append).start()
    for _ in range(400):
        assert pool.try_submit(lambda: None)
    assert pool.shutdown(timeout=10)
    m = pool.metrics()
    assert m["completed"] == 400  # 여러 워커가 동시에 갱신해도 정확
    assert sum(m["latency_histogram"].values()) == 400
    assert m["active_workers"] == 0


@pytest.mark.parametrize(("n", "q"), [(0, 1), (1, 0)])
def test_invalid_sizes(n, q):
    with pytest.raises(ValueError):
        WorkerPool(n, q)
