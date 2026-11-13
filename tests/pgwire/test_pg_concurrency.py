"""PG 연결 여러 개로 스냅샷 불변식 (결과가 1..k 연속, k 비감소)과 큐가 가득 찼을 때의 동작."""

import threading

import pytest
from pgclient import SYNC, PgClient, bind, execute, kinds, parse, query


@pytest.mark.stress
def test_snapshot_invariant_over_pg(server, stress_scale):
    W, R = 4, 3
    per_writer = 150 * stress_scale
    stop = threading.Event()
    errors: list[BaseException] = []
    checks = [0] * R

    def writer(i):
        c = PgClient(server.pg_address)
        try:
            c.startup()
            c.send(parse("INSERT INTO users (name, age) VALUES ($1, $2)", "ins") + SYNC)
            assert kinds(c.read_until("Z")) == "1Z"
            for j in range(per_writer):  # 확장 쿼리 (준비된 문장 재사용)
                c.send(bind((f"w{i}".encode(), str(j).encode()), stmt="ins") + execute() + SYNC)
                msgs = c.read_until("Z")
                assert kinds(msgs) == "2CZ", msgs
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
        finally:
            c.close()

    def reader(i):
        c = PgClient(server.pg_address)
        last = 0
        try:
            c.startup()
            sqls = ["SELECT id FROM users", "SELECT id FROM users WHERE id >= 1"]
            n = 0
            while not stop.is_set():
                ids = [int(r[0]) for r in c.rows(sqls[n % 2])]  # 단순 쿼리
                n += 1
                k = len(ids)
                assert ids == list(range(1, k + 1)), f"not a committed prefix: k={k} tail={ids[-5:]}"
                assert k >= last, f"snapshot went backwards ({last} -> {k})"
                last = k
                checks[i] += 1
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
        finally:
            c.close()

    readers = [threading.Thread(target=reader, args=(i,)) for i in range(R)]
    writers = [threading.Thread(target=writer, args=(i,)) for i in range(W)]
    for t in readers + writers:
        t.start()
    for t in writers:
        t.join(120)
        assert not t.is_alive()
    stop.set()
    for t in readers:
        t.join(60)
    assert errors == [], errors[:3]
    assert all(c > 0 for c in checks)
    c = PgClient(server.pg_address)
    c.startup()
    assert len(c.rows("SELECT id FROM users")) == W * per_writer
    c.close()


@pytest.fixture
def busy_server(server_factory):
    """워커 1개 + 큐 1칸을 막아 둔 서버. gate 를 set 하면 풀린다."""
    s = server_factory(workers=1, queue_size=1)
    gate = threading.Event()
    started = threading.Event()

    def block():
        started.set()
        gate.wait(30)

    yield s, gate, started, block
    gate.set()


def test_queue_full_sends_53000_and_keeps_connection(busy_server):
    s, gate, started, block = busy_server
    c = PgClient(s.pg_address)
    try:
        c.startup()
        assert s.pool.try_submit(block)
        started.wait(5)
        assert s.pool.try_submit(block)  # 큐도 가득
        c.send(query("SELECT * FROM users"))
        msgs = c.read_until("Z")
        assert kinds(msgs) == "EZ"
        f = msgs[0].fields()
        assert (f["S"], f["C"]) == ("ERROR", "53000")
        # 확장 쿼리: 첫 메시지에 오류, Sync 까지 버림, ReadyForQuery
        c.send(parse("SELECT * FROM users") + bind() + execute() + SYNC)
        assert kinds(c.read_until("Z")) == "EZ"
        gate.set()
        assert c.rows("SHOW server_version") == [["16.0"]]  # 같은 연결로 계속 쓸 수 있다
        assert s.pg.stats()["rejected_busy"] >= 2
    finally:
        c.close()


def test_queue_full_during_startup_is_fatal_53300(busy_server):
    s, gate, started, block = busy_server
    assert s.pool.try_submit(block)
    started.wait(5)
    assert s.pool.try_submit(block)
    c = PgClient(s.pg_address)
    try:
        from pgclient import startup_packet

        c.send(startup_packet({"user": "x", "database": "minidb"}))
        f = c.read().fields()
        assert (f["S"], f["C"], f["M"]) == ("FATAL", "53300", "sorry, too many clients already")
        assert c.is_closed()
    finally:
        c.close()


def test_max_connections_fatal(server_factory):
    s = server_factory(max_connections=1)
    a = PgClient(s.pg_address)
    a.startup()
    b = PgClient(s.pg_address)
    try:
        f = b.read().fields()
        assert (f["S"], f["C"]) == ("FATAL", "53300")
    finally:
        a.close()
        b.close()


def test_shutdown_sends_admin_shutdown_to_idle_connections(server_factory):
    s = server_factory()
    c = PgClient(s.pg_address)
    c.startup()
    s.shutdown(timeout=10)
    f = c.read().fields()
    assert (f["S"], f["C"], f["M"]) == ("FATAL", "57P01", "terminating connection due to administrator command")
    c.close()


def test_stats_include_pg(server, pg):
    st = server.stats()["pg"]
    assert st["connections"] == 1 and st["sessions"] == 1 and st["running_queries"] == 0
    assert st["database"] == "minidb"
