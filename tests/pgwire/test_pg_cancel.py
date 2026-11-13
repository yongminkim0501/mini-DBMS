"""쿼리 취소: 100만 건 Seq Scan 도중 CancelRequest → 57014, 틀린 키는 무시, 모든 워커가 바쁠 때도 동작."""

import io
import socket
import time

import pytest
from conftest import make_db
from pgclient import PgClient, cancel_packet, kinds, query

from minidb.engine import Database
from minidb.server import Server, ServerConfig

ROWS = 1_000_000
LONG_SCAN = "SELECT * FROM users WHERE age = 1000"  # 일치하는 행 없음: 100만 건을 끝까지 훑는다


@pytest.fixture(scope="module")
def big_db(tmp_path_factory):
    path = make_db(tmp_path_factory.mktemp("big") / "db")
    with Database(path) as db:
        h = db.table_handle(db.catalog.get("users"))
        for b in range(10):
            h.append_many([(f"user{b * 100_000 + i}", i % 100) for i in range(100_000)])
    return path


@pytest.fixture
def big_server(big_db):
    servers = []

    def make(workers=4):
        s = Server(big_db, ServerConfig(port=None, pg_port=0, workers=workers), log_stream=io.StringIO()).start()
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.shutdown(timeout=30)


def wait_running(server, n=1, timeout=30.0):
    deadline = time.monotonic() + timeout
    while server.pg.stats()["running_queries"] < n:
        assert time.monotonic() < deadline, "query did not start"
        time.sleep(0.005)


def send_cancel(server, pid, key):
    s = socket.create_connection(server.pg_address, timeout=10)
    try:
        s.sendall(cancel_packet(pid, key))
        assert s.recv(16) == b""  # 응답 없이 닫힌다 (PostgreSQL 과 같다)
    finally:
        s.close()


def connect(server):
    c = PgClient(server.pg_address, timeout=60)
    c.startup()
    return c


def test_cancel_long_seq_scan_then_connection_still_usable(big_server):
    server = big_server()
    c = connect(server)
    try:
        c.rows("SELECT id FROM users WHERE id = 1")  # 테이블을 미리 연다 (인덱스 구성 시간 제외)
        t0 = time.monotonic()
        c.send(query(LONG_SCAN))
        wait_running(server)
        pid, key = c.backend_key
        send_cancel(server, pid, key)
        msgs = c.read_until("Z")
        elapsed = time.monotonic() - t0
        assert kinds(msgs) == "TEZ"  # RowDescription 은 스캔 시작 전에 이미 나갔다 (PostgreSQL 과 같다)
        f = msgs[1].fields()
        assert (f["C"], f["M"]) == ("57014", "canceling statement due to user request")
        assert elapsed < 10
        # 같은 연결로 다음 쿼리는 정상 (플래그는 문장이 끝나면 초기화)
        assert c.rows("SELECT name FROM users WHERE id = 1000000") == [["user999999"]]
        assert server.pg.stats()["cancels_delivered"] == 1
    finally:
        c.close()


def test_wrong_key_is_ignored(big_server):
    server = big_server()
    c = connect(server)
    try:
        c.rows("SELECT id FROM users WHERE id = 1")
        c.send(query(LONG_SCAN))
        wait_running(server)
        pid, key = c.backend_key
        send_cancel(server, pid, key ^ 1)  # 틀린 키
        send_cancel(server, pid + 10_000, key)  # 없는 연결
        msgs = c.read_until("Z")
        assert kinds(msgs) == "TCZ" and msgs[1].tag() == "SELECT 0"  # 끝까지 실행됨
        assert server.pg.stats()["cancels_delivered"] == 0
    finally:
        c.close()


def test_cancel_when_idle_does_nothing(big_server):
    server = big_server()
    c = connect(server)
    try:
        pid, key = c.backend_key
        send_cancel(server, pid, key)
        assert c.rows("SELECT id FROM users WHERE id = 5") == [["5"]]
    finally:
        c.close()


def test_cancel_works_when_all_workers_busy(big_server):
    server = big_server(workers=1)
    a, b = connect(server), connect(server)
    try:
        a.rows("SELECT id FROM users WHERE id = 1")
        a.send(query(LONG_SCAN))  # 하나뿐인 워커를 붙잡는다
        wait_running(server)
        b.send(query("SELECT name FROM users WHERE id = 7"))  # 큐에서 기다린다
        time.sleep(0.05)
        t0 = time.monotonic()
        send_cancel(server, *a.backend_key)  # 워커가 없어도 디스패처가 바로 처리
        assert kinds(a.read_until("Z")) == "TEZ"
        assert time.monotonic() - t0 < 10
        assert [m.text_row() for m in b.read_until("Z") if m.kind == "D"] == [["user6"]]
    finally:
        a.close()
        b.close()


def test_cancel_after_ssl_request(big_server):
    """libpq 17 의 새 취소 API 는 sslmode=prefer 면 SSLRequest 를 먼저 보낸 뒤 CancelRequest 를 보낸다."""
    import struct

    server = big_server()
    c = connect(server)
    try:
        c.rows("SELECT id FROM users WHERE id = 1")
        c.send(query(LONG_SCAN))
        wait_running(server)
        s = socket.create_connection(server.pg_address, timeout=10)
        s.sendall(struct.pack("!iI", 8, 80877103))
        assert s.recv(1) == b"N"
        s.sendall(cancel_packet(*c.backend_key))
        assert s.recv(16) == b""
        s.close()
        assert c.read_until("Z")[1].fields()["C"] == "57014"
    finally:
        c.close()
