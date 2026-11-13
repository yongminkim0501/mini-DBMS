"""HTTP 서버 통합 테스트: 엔드포인트, 상태 코드, SQL 오류 JSON, 세션, 연결/풀 한계, 정상 종료.

테스트마다 임시 DB 와 임시 포트(port=0)로 서버를 띄운다.
"""

import http.client
import io
import json
import socket
import threading
import time

import pytest

from minidb.engine import Database
from minidb.server import Server, ServerConfig
from minidb.server.dispatcher import Response

USERS = {
    "oid": 16384,
    "table": "users",
    "columns": [
        {"name": "id", "type": "BIGINT", "auto_increment": True},
        {"name": "name", "type": "TEXT", "nullable": True},
        {"name": "age", "type": "BIGINT", "nullable": True},
    ],
}


def make_db(path):
    (path / "schema").mkdir(parents=True, exist_ok=True)
    (path / "schema" / "16384.json").write_text(json.dumps(USERS), encoding="utf-8")
    return path


@pytest.fixture
def db_dir(tmp_path):
    return make_db(tmp_path / "db")


@pytest.fixture
def start_server(db_dir):
    servers = []

    def start(**kw):
        cfg = ServerConfig(port=0, workers=kw.pop("workers", 4), **kw)
        s = Server(db_dir, cfg, log_stream=io.StringIO()).start()
        servers.append(s)
        return s

    yield start
    for s in servers:
        s.shutdown(timeout=10)


@pytest.fixture
def server(start_server):
    return start_server()


def call(server, method, path, body=None, headers=None, conn=None):
    c = conn or http.client.HTTPConnection(*server.address, timeout=10)
    if isinstance(body, str):
        body = body.encode("utf-8")
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    if conn is None:
        c.close()
    parsed = json.loads(data) if data and r.getheader("Content-Type", "").startswith("application/json") else data
    return r.status, parsed, r


def q(server, sql, headers=None, conn=None):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    return call(server, "POST", "/query", json.dumps({"sql": sql}), h, conn)


def raw_exchange(server, data: bytes, timeout=5.0) -> bytes:
    """원시 바이트를 보내고 서버가 닫을 때까지(또는 timeout) 받은 것 전부."""
    s = socket.create_connection(server.address, timeout=timeout)
    s.sendall(data)
    out = b""
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            out += chunk
    except TimeoutError:
        pass
    s.close()
    return out


def status_of(raw: bytes) -> int:
    return int(raw.split(b" ", 2)[1])


def body_of(raw: bytes):
    return json.loads(raw.split(b"\r\n\r\n", 1)[1])


# ------------------------------------------------------------------ 기본 엔드포인트


def test_health_and_stats(server):
    st, body, r = call(server, "GET", "/health")
    assert (st, body) == (200, {"status": "ok"})
    assert r.getheader("Content-Type") == "application/json; charset=utf-8"
    st, body, _ = call(server, "GET", "/stats")
    assert st == 200
    rt = body["runtime"]
    assert {"python_version", "free_threaded_build", "gil_enabled"} <= rt.keys()
    assert body["pool"]["workers"] == 4
    assert {"open_connections", "max_connections"} <= body["connections"].keys()
    assert body["uptime_seconds"] >= 0
    assert body["process"]["cpu_user_seconds"] >= 0


def test_query_json_and_text_plain(server):
    st, body, _ = q(server, "INSERT INTO users (name, age) VALUES ('kim', 25), ('한글', NULL) RETURNING id, name")
    assert st == 200 and body["error"] is None
    (r,) = body["results"]
    assert r["command"] == "INSERT 0 2"
    assert r["columns"] == [{"name": "id", "type": "bigint", "type_oid": 20}, {"name": "name", "type": "text", "type_oid": 25}]
    assert r["rows"] == [[1, "kim"], [2, "한글"]] and r["row_count"] == 2
    st, body, _ = call(server, "POST", "/query", "SELECT * FROM users WHERE id = 2", {"Content-Type": "text/plain; charset=utf-8"})
    assert st == 200
    assert body["results"][0] == {
        "command": "SELECT 1",
        "columns": [
            {"name": "id", "type": "bigint", "type_oid": 20},
            {"name": "name", "type": "text", "type_oid": 25},
            {"name": "age", "type": "bigint", "type_oid": 20},
        ],
        "rows": [[2, "한글", None]],
        "row_count": 1,
    }
    # Content-Type 이 없으면 text/plain 으로 본다
    st, body, _ = call(server, "POST", "/query", "SELECT id FROM users")
    assert st == 200 and body["results"][0]["rows"] == [[1], [2]]


def test_bigint_as_json_number(server):
    q(server, "INSERT INTO users (age) VALUES (9223372036854775807)")
    _, body, _ = q(server, "SELECT age FROM users")
    assert body["results"][0]["rows"] == [[9223372036854775807]]
    assert isinstance(body["results"][0]["rows"][0][0], int)


def test_insert_without_returning_and_set_show_explain(server):
    _, body, _ = q(server, "INSERT INTO users (name) VALUES ('a'); SET enable_indexscan = off; SHOW enable_indexscan; EXPLAIN SELECT * FROM users WHERE id = 1")
    r = body["results"]
    assert r[0] == {"command": "INSERT 0 1", "columns": None, "rows": None, "row_count": 1}
    assert r[1] == {"command": "SET", "columns": None, "rows": None, "row_count": None}
    assert r[2]["rows"] == [["off"]] and r[2]["command"] == "SHOW"
    assert r[3]["rows"][0] == ["Seq Scan on users"]  # 같은 요청 안에서는 SET 이 유지된다
    # 세션 없는 다음 요청은 새 기본 세션
    _, body, _ = q(server, "SHOW enable_indexscan")
    assert body["results"][0]["rows"] == [["on"]]


# ------------------------------------------------------------------ SQL 오류


def test_sql_error_json(server):
    sql = "SELECT * FROM users WHERE name = 1"
    st, body, _ = q(server, sql)
    assert st == 400 and body["results"] == []
    e = body["error"]
    assert e["sqlstate"] == "42883"
    assert e["message"] == "operator does not exist: text = integer"
    assert e["position"] == sql.index("=") + 1
    assert e["hint"].startswith("No operator matches")
    assert e["statement_index"] == 0


def test_error_in_middle_of_statements(server):
    sql = "INSERT INTO users (name) VALUES ('a');\nSELECT nope FROM users;\nINSERT INTO users (name) VALUES ('b')"
    st, body, _ = q(server, sql)
    assert st == 400
    assert [r["command"] for r in body["results"]] == ["INSERT 0 1"]  # 앞 문장은 반영되어 결과에 포함
    e = body["error"]
    assert e["sqlstate"] == "42703" and e["statement_index"] == 1
    assert e["position"] == sql.index("nope") + 1  # 요청 전체 SQL 기준
    _, body, _ = q(server, "SELECT name FROM users")
    assert body["results"][0]["rows"] == [["a"]]  # 오류 뒤 문장은 실행되지 않았다


def test_unique_detail_field_present(server):
    _, body, _ = q(server, "INSERT INTO users (id) VALUES (5)")
    e = body["error"]
    assert e["sqlstate"] == "428C9" and e["detail"] == 'Column "id" is an identity column defined as GENERATED ALWAYS.'


def test_max_result_rows(start_server):
    s = start_server(max_result_rows=5)
    q(s, "INSERT INTO users (name) VALUES " + ", ".join(["('x')"] * 6))
    st, body, _ = q(s, "SELECT * FROM users LIMIT 5")
    assert st == 200 and body["results"][0]["row_count"] == 5
    st, body, _ = q(s, "SELECT * FROM users")
    assert st == 400
    assert body["error"]["sqlstate"] == "54000"
    assert "max_result_rows (5)" in body["error"]["message"] and "LIMIT" in body["error"]["hint"]


# ------------------------------------------------------------------ 라우팅/프로토콜 상태 코드


def test_status_codes(server):
    st, body, _ = call(server, "GET", "/nope")
    assert st == 404 and body["error"]["http_status"] == 404
    st, _, r = call(server, "GET", "/query")
    assert st == 405 and r.getheader("Allow") == "POST"
    st, _, r = call(server, "POST", "/health")
    assert st == 405 and r.getheader("Allow") == "GET, HEAD"
    st, body, _ = call(server, "POST", "/query", "<x/>", {"Content-Type": "application/xml"})
    assert st == 415
    st, _, _ = call(server, "POST", "/query", "SELECT 1", {"Content-Type": "text/plain; charset=latin-1"})
    assert st == 415
    st, body, _ = call(server, "POST", "/query", "{bad json", {"Content-Type": "application/json"})
    assert st == 400
    st, body, _ = call(server, "POST", "/query", '{"query": "x"}', {"Content-Type": "application/json"})
    assert st == 400 and "sql" in body["error"]["message"]


def test_protocol_errors(start_server):
    s = start_server(max_body_bytes=100)
    raw = raw_exchange(s, b"POST /query HTTP/1.1\r\nHost: x\r\n\r\n")
    assert status_of(raw) == 411 and b"Connection: close" in raw
    raw = raw_exchange(s, b"POST /query HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n")
    assert status_of(raw) == 501
    raw = raw_exchange(s, b"POST /query HTTP/1.1\r\nHost: x\r\nContent-Length: 1000\r\n\r\n")
    assert status_of(raw) == 413 and body_of(raw)["error"]["http_status"] == 413
    raw = raw_exchange(s, b"GARBAGE\r\n\r\n")
    assert status_of(raw) == 400
    raw = raw_exchange(s, b"GET / HTTP/1.1\r\nHost: x\r\nX: " + b"a" * 20000 + b"\r\n\r\n")
    assert status_of(raw) == 431


def test_internal_error_isolated(server):
    real = server.dispatcher.handler

    def handler(req):
        if req.path == "/boom":
            raise RuntimeError("bug")
        return real(req)

    server.dispatcher.handler = handler
    st, body, _ = call(server, "GET", "/boom")
    assert st == 500 and body["error"]["sqlstate"] == "XX000"
    st, _, _ = call(server, "GET", "/health")  # 서버와 워커는 계속 동작
    assert st == 200


def test_engine_internal_error_is_500(server, monkeypatch):
    def broken(self, *a, **k):
        raise RuntimeError("engine bug")

    monkeypatch.setattr(Database, "execute_one", broken)
    st, body, _ = q(server, "SELECT * FROM users")
    assert st == 500 and body["error"]["sqlstate"] == "XX000" and "engine bug" in body["error"]["message"]


# ------------------------------------------------------------------ keep-alive / 파이프라이닝 / 타임아웃


def test_keep_alive_reuses_connection(server):
    c = http.client.HTTPConnection(*server.address, timeout=10)
    for i in range(5):
        st, _, r = q(server, f"INSERT INTO users (age) VALUES ({i})", conn=c)
        assert st == 200 and r.getheader("Connection") == "keep-alive"
    assert server.dispatcher.stats()["accepted_total"] == 1
    st, _, r = call(server, "GET", "/health", headers={"Connection": "close"}, conn=c)
    assert r.getheader("Connection") == "close"
    c.close()


def test_pipelining_two_requests_in_one_packet(server):
    b = b"GET /health HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
    body = b"INSERT INTO users (name) VALUES ('p1')"
    a = b"POST /query HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body)
    raw = raw_exchange(server, a + b)
    first, rest = raw.split(b"\r\n\r\n", 1)
    assert first.startswith(b"HTTP/1.1 200")
    assert b'"INSERT 0 1"' in rest and b"HTTP/1.1 200 OK" in rest and rest.endswith(b'{"status": "ok"}')


def test_request_timeout_408(start_server):
    s = start_server(request_timeout=0.3)
    t0 = time.monotonic()
    raw = raw_exchange(s, b"POST /query HTTP/1.1\r\nHost: x\r\nContent-Length: 50\r\n\r\nSELECT", timeout=5)
    assert status_of(raw) == 408
    assert 0.25 < time.monotonic() - t0 < 4
    assert s.dispatcher.stats()["request_timeouts"] == 1
    st, _, _ = call(s, "GET", "/health")  # 워커는 풀려났다
    assert st == 200


def test_idle_connection_closed_and_does_not_hold_worker(start_server):
    s = start_server(workers=1, idle_timeout=0.4)
    idle = [socket.create_connection(s.address) for _ in range(5)]  # 워커 1개보다 많은 유휴 연결
    st, _, _ = call(s, "GET", "/health")  # 유휴 연결이 워커를 붙잡지 않는다
    assert st == 200
    time.sleep(1.2)
    for sock in idle:
        sock.settimeout(2)
        assert sock.recv(10) == b""  # 서버가 닫았다
        sock.close()
    assert s.dispatcher.stats()["open_connections"] == 0


def test_max_connections_503(start_server):
    s = start_server(max_connections=2)
    keep = [socket.create_connection(s.address) for _ in range(2)]
    time.sleep(0.1)
    raw = raw_exchange(s, b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
    assert status_of(raw) == 503 and body_of(raw)["error"]["sqlstate"] == "53300"
    for k in keep:
        k.close()
    deadline = time.monotonic() + 5
    while s.dispatcher.stats()["open_connections"] > 0:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert call(s, "GET", "/health")[0] == 200


def _blocking_handler(server):
    gate = threading.Event()
    entered = threading.Semaphore(0)
    real = server.dispatcher.handler

    def handler(req):
        if req.path == "/block":
            entered.release()
            gate.wait(10)
            return Response(200, b'{"blocked": true}')
        return real(req)

    server.dispatcher.handler = handler
    return gate, entered


def test_pool_full_503_with_retry_after(start_server):
    s = start_server(workers=1, queue_size=1)
    gate, entered = _blocking_handler(s)
    results = []

    def bg():
        results.append(call(s, "GET", "/block")[0])

    t1 = threading.Thread(target=bg)
    t1.start()
    assert entered.acquire(timeout=5)  # 워커 1개가 막혔다
    t2 = threading.Thread(target=bg)
    t2.start()
    deadline = time.monotonic() + 5
    while s.pool.metrics()["queue_length"] < 1:  # 두 번째 요청이 큐 1칸을 차지
        assert time.monotonic() < deadline
        time.sleep(0.01)
    st, body, r = call(s, "GET", "/health")
    assert st == 503 and r.getheader("Retry-After") == "1"
    assert body["error"]["sqlstate"] == "53000"
    # gate 를 열기 전에 센다 (연 뒤에는 첫 연결이 닫히며 생긴 EOF 이벤트도 큐가 찬 순간에 오면 거부로 셀 수 있다)
    assert s.dispatcher.stats()["rejected_busy"] == 1
    gate.set()
    t1.join(10)
    t2.join(10)
    assert results == [200, 200]  # 이미 받은 요청은 거부되지 않고 처리된다


# ------------------------------------------------------------------ 세션


def test_sessions_keep_settings_and_isolate(server):
    st, body, _ = call(server, "POST", "/sessions")
    assert st == 201
    a = body["session_id"]
    b = call(server, "POST", "/sessions")[1]["session_id"]
    assert a != b and len(a) == 32
    q(server, "SET enable_indexscan = off", {"X-Session-Id": a})
    assert q(server, "SHOW enable_indexscan", {"X-Session-Id": a})[1]["results"][0]["rows"] == [["off"]]
    assert q(server, "SHOW enable_indexscan", {"X-Session-Id": b})[1]["results"][0]["rows"] == [["on"]]
    assert q(server, "SHOW enable_indexscan")[1]["results"][0]["rows"] == [["on"]]
    st, body, _ = call(server, "DELETE", f"/sessions/{a}")
    assert st == 204 and body == b""
    st, body, _ = call(server, "DELETE", f"/sessions/{a}")
    assert st == 404
    st, body, _ = q(server, "SELECT 1 FROM users", {"X-Session-Id": a})
    assert st == 404 and "does not exist" in body["error"]["message"]
    assert call(server, "GET", f"/sessions/{b}")[0] == 405


def test_session_expiry(start_server):
    s = start_server(session_timeout=0.3)
    sid = call(s, "POST", "/sessions")[1]["session_id"]
    assert q(s, "SHOW enable_indexscan", {"X-Session-Id": sid})[0] == 200
    time.sleep(0.5)
    assert q(s, "SHOW enable_indexscan", {"X-Session-Id": sid})[0] == 404
    assert call(s, "GET", "/stats")[1]["sessions"]["expired_total"] >= 1


def test_same_session_requests_are_serialized(server):
    sid = call(server, "POST", "/sessions")[1]["session_id"]
    entry = server.sessions.get(sid)
    inside = []
    max_inside = [0]
    lock = threading.Lock()
    real_run = server.api._run

    def tracking_run(sql, session):
        with lock:
            inside.append(1)
            max_inside[0] = max(max_inside[0], len(inside))
        time.sleep(0.02)
        try:
            return real_run(sql, session)
        finally:
            with lock:
                inside.pop()

    server.api._run = tracking_run
    ts = [threading.Thread(target=q, args=(server, "SHOW enable_indexscan", {"X-Session-Id": sid})) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert max_inside[0] == 1  # 같은 세션의 요청은 동시에 실행되지 않는다
    assert entry.session is server.sessions.get(sid).session


# ------------------------------------------------------------------ 정상 종료


def test_graceful_shutdown_finishes_inflight_and_persists(db_dir):
    s = Server(db_dir, ServerConfig(port=0, workers=2), log_stream=io.StringIO()).start()
    q(s, "INSERT INTO users (name) VALUES ('before')")
    gate, entered = _blocking_handler(s)
    result = {}

    def inflight():
        result["resp"] = call(s, "GET", "/block")

    t = threading.Thread(target=inflight)
    t.start()
    assert entered.acquire(timeout=5)
    idle = socket.create_connection(s.address)  # 유휴 keep-alive 연결
    time.sleep(0.1)
    done = {}
    sd = threading.Thread(target=lambda: done.setdefault("clean", s.shutdown(timeout=10)))
    sd.start()
    s.dispatcher.stopped.wait(5)
    with pytest.raises(OSError):  # 리스닝 소켓이 닫혀 새 연결은 거부된다
        socket.create_connection(s.address, timeout=1)
    idle.settimeout(2)
    assert idle.recv(10) == b""  # 유휴 연결은 닫혔다
    assert sd.is_alive()  # 처리 중인 요청을 기다리는 중
    gate.set()
    t.join(10)
    sd.join(10)
    st, body, r = result["resp"]
    assert st == 200 and body == {"blocked": True}
    assert r.getheader("Connection") == "close"
    assert done["clean"] is True
    with Database(db_dir) as db:  # 재시작 후 데이터 일치
        assert db.execute_one("SELECT name FROM users").fetchall() == [("before",)]
