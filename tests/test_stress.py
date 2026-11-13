"""동시성 정확성 스트레스 테스트 (마커 ``stress``). 기본 크기는 CI 용으로 작게, ``--stress-scale N`` 으로 키운다.

스냅샷 불변식: INSERT 는 배타 락 안에서 직렬화되고 id 는 1 부터 순차로 붙으므로, 올바른 스냅샷 읽기의 결과는 항상
"커밋된 접두사" — 정확히 ``1..k`` 의 연속 구간 — 이다. 읽기 스레드마다 k 는 줄어들지 않아야 한다(시간이 지나면
커밋된 접두사는 길어지기만 한다). 이 검사가 잡는 버그:

- 반쯤 쓰인 레코드/아직 인덱스에 없는 레코드를 읽음 (락 밖에서 _end 를 읽거나, append 와 인덱스 갱신이 다른 구간)
- 한 문장 안에서 서로 다른 시점을 섞어 읽음 (스냅샷 없이 파일 끝까지 읽기 → 중간에 커밋된 행이 섞여 구멍/중복)
- id 중복 할당 (핸들 캐시 경쟁, next_id 경쟁) → 중복 또는 구멍
- 스냅샷이 과거로 돌아감 (k 감소)
"""

import io
import json
import random
import threading
import time

import pytest

from minidb.engine import Database
from minidb.errors import DatabaseError
from minidb.server import Server, ServerConfig
from minidb.session import Session

pytestmark = pytest.mark.stress

USERS = {
    "oid": 16384,
    "table": "users",
    "columns": [
        {"name": "id", "type": "BIGINT", "auto_increment": True},
        {"name": "name", "type": "TEXT", "nullable": True},
        {"name": "age", "type": "BIGINT", "nullable": True},
    ],
}


@pytest.fixture
def db_dir(tmp_path):
    (tmp_path / "schema").mkdir()
    (tmp_path / "schema" / "16384.json").write_text(json.dumps(USERS), encoding="utf-8")
    return tmp_path


def run_all(threads, timeout=120):
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout)
        assert not t.is_alive(), "thread did not finish (deadlock?)"


def check_consistent(db, expected_n):
    h = db.table_handle(db.catalog.get("users"))
    file_entries = [(row[0], off) for off, row in h.file.scan()]
    assert [k for k, _ in file_entries] == list(range(1, expected_n + 1))  # 중복·구멍 없음
    assert list(h.index.items()) == file_entries  # 인덱스 = 파일
    h.index.check_invariants()


def assert_prefix(ids, last_k, where):
    k = len(ids)
    assert ids == list(range(1, k + 1)), f"{where}: not a committed prefix (k={k}, head={ids[:5]}, tail={ids[-5:]})"
    assert k >= last_k, f"{where}: snapshot went backwards ({last_k} -> {k})"
    return k


# ------------------------------------------------------------------ 엔진 직접


def test_concurrent_inserts_assign_unique_contiguous_ids(db_dir, stress_scale):
    T, M = 8, 400 * stress_scale
    errors = []

    def writer(i):
        sess = Session()
        try:
            with_batch = i % 2 == 0  # 단건과 다건 INSERT 를 섞는다
            done = 0
            while done < M:
                n = min(3, M - done) if with_batch else 1
                vals = ", ".join(f"('t{i}', {done + j})" for j in range(n))
                r = db.execute_one(f"INSERT INTO users (name, age) VALUES {vals}", sess)
                assert r.command_tag == f"INSERT 0 {n}"
                done += n
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    db = Database(db_dir)
    # 테이블을 아무도 열지 않은 상태에서 동시에 시작한다 (핸들 캐시 생성 경쟁도 함께 검사)
    run_all([threading.Thread(target=writer, args=(i,)) for i in range(T)])
    assert errors == []
    check_consistent(db, T * M)
    rows = db.execute_one("SELECT name, age FROM users").fetchall()
    for i in range(T):  # 스레드마다 자기 행들이 자기 순서대로 들어갔다
        assert [a for n, a in rows if n == f"t{i}"] == list(range(M))
    db.close()
    with Database(db_dir) as db2:  # 재오픈 후에도 일치
        check_consistent(db2, T * M)


def test_snapshot_reads_see_committed_prefix(db_dir, stress_scale):
    W, R = 4, 4
    per_writer = 400 * stress_scale
    db = Database(db_dir)
    db.execute_one("INSERT INTO users (name) VALUES ('seed')")
    stop = threading.Event()
    errors = []
    checks = [0] * R
    queries = [
        "SELECT id FROM users",  # Seq Scan
        "SELECT id FROM users WHERE id >= 1",  # 범위 Index Scan (클러스터드)
        "SELECT id FROM users WHERE id > 0 AND id < 9223372036854775807",
        "SELECT id FROM users WHERE name <> 'none'",  # Seq Scan + Filter (모든 행의 name 은 NULL 이 아님)
    ]

    def writer(i):
        sess = Session()
        try:
            for j in range(per_writer):
                db.execute_one(f"INSERT INTO users (name, age) VALUES ('w{i}', {j})", sess)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    def reader(i):
        sess = Session()
        last = 0
        try:
            while not stop.is_set():
                sql = queries[i % len(queries)]
                ids = [r[0] for r in db.execute_one(sql, sess).fetchall()]
                last = assert_prefix(ids, last, sql)
                # 점 검색: 방금 본 접두사의 마지막 id 는 항상 보여야 한다
                got = db.execute_one(f"SELECT id FROM users WHERE id = {last}", sess).fetchall()
                assert got == [(last,)], f"point lookup of committed id {last} returned {got}"
                checks[i] += 1
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    readers = [threading.Thread(target=reader, args=(i,)) for i in range(R)]
    writers = [threading.Thread(target=writer, args=(i,)) for i in range(W)]
    for t in readers:
        t.start()
    run_all(writers)
    stop.set()
    for t in readers:
        t.join(60)
    assert errors == [], errors[:3]
    assert all(c > 0 for c in checks), checks
    check_consistent(db, 1 + W * per_writer)
    db.close()


def test_writer_not_starved_by_continuous_readers(db_dir, stress_scale):
    db = Database(db_dir)
    db.execute_one("INSERT INTO users (name) VALUES " + ", ".join(["('r')"] * 2000))
    stop = threading.Event()
    errors = []
    reads = [0] * 6

    def reader(i):
        sess = Session()
        try:
            while not stop.is_set():
                db.execute_one("SELECT * FROM users WHERE age = 1", sess).fetchall()  # Seq Scan 반복
                reads[i] += 1
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    readers = [threading.Thread(target=reader, args=(i,)) for i in range(6)]
    for t in readers:
        t.start()
    time.sleep(0.05)
    n = 50 * stress_scale
    t0 = time.monotonic()
    sess = Session()
    for _ in range(n):
        db.execute_one("INSERT INTO users (name) VALUES ('w')", sess)
    elapsed = time.monotonic() - t0
    stop.set()
    for t in readers:
        t.join(60)
    assert errors == []
    assert sum(reads) > 0
    assert elapsed < 30, f"{n} inserts took {elapsed:.1f} s under read load"
    check_consistent(db, 2000 + n)
    db.close()


def test_lock_timeout_reports_55p03(db_dir):
    db = Database(db_dir, lock_timeout=0.1)
    db.execute_one("INSERT INTO users (name) VALUES ('a')")
    h = db.table_handle(db.catalog.get("users"))
    h.lock.acquire_write()
    result = {}

    def other():
        try:
            db.execute_one("SELECT * FROM users").fetchall()
        except DatabaseError as e:
            result["e"] = e

    t = threading.Thread(target=other)
    t.start()
    t.join(5)
    h.lock.release_write()
    assert result["e"].sqlstate == "55P03"
    assert result["e"].message == 'could not obtain lock on relation "users"'
    db.close()


# ------------------------------------------------------------------ HTTP 경유


def test_http_concurrent_inserts_and_snapshot_reads(db_dir, stress_scale):
    import http.client

    server = Server(db_dir, ServerConfig(port=0, workers=6, queue_size=256), log_stream=io.StringIO()).start()
    W, R = 4, 3
    per_writer = 150 * stress_scale
    stop = threading.Event()
    errors = []
    checks = [0] * R

    def post(conn, sql):
        conn.request("POST", "/query", body=sql.encode(), headers={"Content-Type": "text/plain"})
        r = conn.getresponse()
        body = json.loads(r.read())
        assert r.status == 200, body
        return body["results"]

    def writer(i):
        conn = http.client.HTTPConnection(*server.address, timeout=30)
        try:
            for j in range(per_writer):
                res = post(conn, f"INSERT INTO users (name, age) VALUES ('h{i}', {j}) RETURNING id")
                assert res[0]["row_count"] == 1
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
        finally:
            conn.close()

    def reader(i):
        conn = http.client.HTTPConnection(*server.address, timeout=30)
        last = 0
        rng = random.Random(i)
        try:
            while not stop.is_set():
                sql = rng.choice(["SELECT id FROM users", "SELECT id FROM users WHERE id >= 1"])
                ids = [row[0] for row in post(conn, sql)[0]["rows"]]
                last = assert_prefix(ids, last, sql)
                checks[i] += 1
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
        finally:
            conn.close()

    readers = [threading.Thread(target=reader, args=(i,)) for i in range(R)]
    for t in readers:
        t.start()
    try:
        run_all([threading.Thread(target=writer, args=(i,)) for i in range(W)])
    finally:
        stop.set()
        for t in readers:
            t.join(60)
        server.shutdown(timeout=30)
    assert errors == [], errors[:3]
    assert all(c > 0 for c in checks), checks
    with Database(db_dir) as db:
        check_consistent(db, W * per_writer)
