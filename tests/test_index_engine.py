"""인덱스 연동: 접근 경로 선택, 차등 테스트(enable_indexscan on/off), 재시작/복구, 스캔 횟수, EXPLAIN."""

import json
import random
import warnings
from collections import Counter

import pytest

import minidb.storage as storage_mod
from minidb.binder import bind
from minidb.engine import Database
from minidb.errors import UniqueViolation
from minidb.executor import Filter, IndexScan, Limit, SeqScan, explain_lines
from minidb.index import BPlusTree
from minidb.planner import choose_access_path, merge_id_conditions, plan_select
from minidb.session import Session
from minidb.sql.parser import parse_statement
from minidb.storage import BIGINT_MAX, BIGINT_MIN, RECORD_HEADER_SIZE, TableFile
from minidb.table import TableHandle, index_name_for

USERS = {
    "oid": 16384,
    "table": "users",
    "columns": [
        {"name": "id", "type": "BIGINT", "auto_increment": True},
        {"name": "name", "type": "TEXT", "nullable": True},
        {"name": "age", "type": "BIGINT", "nullable": True},
    ],
}

OFF = Session(enable_indexscan=False)
ON = Session()


def make_db(tmp_path, rows=0, **kw):
    (tmp_path / "schema").mkdir(exist_ok=True)
    (tmp_path / "schema" / "16384.json").write_text(json.dumps(USERS), encoding="utf-8")
    db = Database(tmp_path, **kw)
    if rows:
        insert_rows(db, [(f"u{i}", i % 50) for i in range(rows)])
    return db


def insert_rows(db, rows, batch=500):
    for i in range(0, len(rows), batch):
        part = rows[i : i + batch]
        vals = ", ".join(
            "(" + ("NULL" if n is None else "'" + n.replace("'", "''") + "'") + ", " + ("NULL" if a is None else str(a)) + ")"
            for n, a in part
        )
        db.execute_one(f"INSERT INTO users (name, age) VALUES {vals}")


@pytest.fixture
def db(tmp_path):
    d = make_db(tmp_path, rows=300)
    yield d
    d.close()


def access_path(db, where, session=ON):
    bound = bind(parse_statement(f"SELECT * FROM users WHERE {where}"), db.catalog)
    return choose_access_path(bound, db.table_handle(bound.table), session)


def plan_text(db, sql, session=ON):
    return [r[0] for r in db.execute_one("EXPLAIN " + sql, session).fetchall()]


def ids(db, sql, session=ON):
    return [r[0] for r in db.execute_one(sql, session).fetchall()]


# ------------------------------------------------------------------ 접근 경로 선택


def test_point_lookup(db):
    node = access_path(db, "id = 42")
    assert isinstance(node, IndexScan) and node.point == 42 and not node.empty
    node = access_path(db, "42 = id")  # 파서가 뒤집어 정규화
    assert isinstance(node, IndexScan) and node.point == 42
    node = access_path(db, "id = '42'")  # 문자열 → bigint 변환된 값
    assert isinstance(node, IndexScan) and node.point == 42
    assert ids(db, "SELECT * FROM users WHERE id = 42") == [42]
    assert ids(db, "SELECT * FROM users WHERE id = 100000") == []


@pytest.mark.parametrize(
    ("where", "lo", "lo_inc", "hi", "hi_inc"),
    [
        ("id > 10", 10, False, None, True),
        ("id >= 10", 10, True, None, True),
        ("id < 10", None, True, 10, False),
        ("id <= 10", None, True, 10, True),
        ("id >= 10 AND id < 20", 10, True, 20, False),
        ("id > 5 AND id > 10 AND id >= 7", 10, False, None, True),  # 하한은 가장 큰 값
        ("id < 50 AND id <= 30 AND id < 40", None, True, 30, True),  # 상한은 가장 작은 값
        ("id >= 10 AND id > 10", 10, False, None, True),  # 같은 값이면 미포함이 이긴다
        ("id > 10 AND id >= 10", 10, False, None, True),
        ("id <= 20 AND id < 20", None, True, 20, False),
        ("id < 20 AND id <= 20", None, True, 20, False),
        ("10 < id AND 20 >= id", 10, False, 20, True),
        ("id > 5 AND id < 1180591620717411303424", 5, False, None, True),  # 2^70 상한은 없음과 같다
        ("id > -1180591620717411303424 AND id <= 9", None, True, 9, True),
        (f"id >= {BIGINT_MAX}", BIGINT_MAX, True, None, True),
        (f"id <= {BIGINT_MIN}", None, True, BIGINT_MIN, True),
    ],
)
def test_range_merge(db, where, lo, lo_inc, hi, hi_inc):
    node = access_path(db, where)
    assert isinstance(node, IndexScan) and not node.empty and node.point is None
    assert (node.lo, node.lo_inclusive, node.hi, node.hi_inclusive) == (lo, lo_inc, hi, hi_inc)


@pytest.mark.parametrize(
    "where",
    ["id >= 10 AND id <= 10", "id = 10 AND id >= 5", "id = 10 AND id <= 10 AND id < 11", "id = 10 AND id = 10"],
)
def test_range_collapses_to_point(db, where):
    node = access_path(db, where)
    assert isinstance(node, IndexScan) and node.point == 10


@pytest.mark.parametrize(
    "where",
    [
        "id > 20 AND id < 10",
        "id > 10 AND id < 10",
        "id >= 10 AND id < 10",
        "id > 10 AND id <= 10",
        "id = 3 AND id = 4",
        "id = 3 AND id > 3",
        "id > 1180591620717411303424",  # 2^70: bigint 최댓값보다 크다
        "id >= 1180591620717411303424",
        "id = 1180591620717411303424",
        "id < -1180591620717411303424",
        f"id > {BIGINT_MAX}",
        f"id < {BIGINT_MIN}",
    ],
)
def test_contradictions_are_empty(db, where):
    node = access_path(db, where)
    assert isinstance(node, IndexScan) and node.empty
    assert ids(db, f"SELECT * FROM users WHERE {where}") == []
    assert ids(db, f"SELECT * FROM users WHERE {where}", OFF) == []


def test_empty_index_scan_reads_nothing(db, monkeypatch):
    monkeypatch.setattr(TableFile, "read_at", lambda *a: pytest.fail("read_at called"))
    monkeypatch.setattr(BPlusTree, "range", lambda *a: pytest.fail("range called"))
    monkeypatch.setattr(BPlusTree, "search", lambda *a: pytest.fail("search called"))
    assert ids(db, "SELECT * FROM users WHERE id > 5 AND id < 3") == []


@pytest.mark.parametrize(
    "where",
    [
        "age = 3",
        "name = 'u1'",
        "id <> 5",
        "id <> 5 AND age > 3",
        "id < 1180591620717411303424",  # 상한 없음 + 하한 없음 → 경계 없음
        "id > -1180591620717411303424",
        "id = NULL",
        "id = 5 AND age = NULL",  # NULL 비교가 있으면 조건 전체가 항상 거짓 (스캔 없이 끝남)
    ],
)
def test_seqscan_cases(db, where):
    assert isinstance(access_path(db, where), SeqScan)


def test_mixed_conditions_use_index_and_filter(db):
    plan = plan_select(bind(parse_statement("SELECT * FROM users WHERE age > 10 AND id <> 7 AND id < 20"), db.catalog), db.table_handle, ON)
    assert isinstance(plan.child, Filter)
    scan = plan.child.child
    assert isinstance(scan, IndexScan)
    assert [c.op for c in scan.index_conds] == ["<"]
    assert len(plan.child.conditions) == 3  # Filter 는 모든 조건을 다시 검사
    expected = [i for i in range(1, 20) if i != 7 and (i - 1) % 50 > 10]
    assert ids(db, "SELECT * FROM users WHERE age > 10 AND id <> 7 AND id < 20") == expected


def test_enable_indexscan_off_always_seqscan(db):
    for where in ("id = 1", "id > 3 AND id < 9", "id > 5 AND id < 3"):
        assert isinstance(access_path(db, where, OFF), SeqScan)


def test_merge_id_conditions_ignores_other_columns(db):
    bound = bind(parse_statement("SELECT * FROM users WHERE age = 5 AND name = 'x'"), db.catalog)
    assert merge_id_conditions(bound.where) == (None, ())


# ------------------------------------------------------------------ 실행 (LIMIT, 순서)


def test_range_scan_limit_stops_early(db):
    plan = plan_select(bind(parse_statement("SELECT * FROM users WHERE id > 100 LIMIT 5"), db.catalog), db.table_handle, ON)
    assert isinstance(plan, Limit)
    assert [r[0] for r in plan] == [101, 102, 103, 104, 105]
    scan = plan.child.child.child
    assert isinstance(scan, IndexScan) and scan.rows_fetched == 5


def test_range_results_in_id_order_and_inclusive(db):
    assert ids(db, "SELECT * FROM users WHERE id >= 10 AND id < 15") == [10, 11, 12, 13, 14]
    assert ids(db, "SELECT * FROM users WHERE id > 10 AND id <= 15") == [11, 12, 13, 14, 15]
    assert ids(db, "SELECT * FROM users WHERE id < 3") == [1, 2]
    assert ids(db, "SELECT * FROM users WHERE id >= 299") == [299, 300]


def test_insert_updates_index(db):
    r = db.execute_one("INSERT INTO users (name) VALUES ('new1'), ('new2') RETURNING id")
    assert r.fetchall() == [(301,), (302,)]
    h = db.table_handle(db.catalog.get("users"))
    assert len(h.index) == 302
    assert db.execute_one("SELECT name FROM users WHERE id = 302").fetchall() == [("new2",)]
    h.index.check_invariants()


def test_failed_insert_leaves_index_unchanged(tmp_path):
    db = make_db(tmp_path, rows=10, max_text_bytes=10)
    with db:
        h = db.table_handle(db.catalog.get("users"))
        before = list(h.index.items())
        with pytest.raises(Exception):
            db.execute_one("INSERT INTO users (name) VALUES ('ok'), ('" + "x" * 50 + "')")
        assert list(h.index.items()) == before


# ------------------------------------------------------------------ 차등 테스트


def _random_where(rng, max_id):
    conds = []
    for _ in range(rng.randint(0, 3)):
        kind = rng.random()
        op = rng.choice(["=", "<>", "<", "<=", ">", ">="])
        if kind < 0.55:
            v = rng.choice(
                [rng.randint(-5, max_id + 5), rng.randint(1, max_id), 0, max_id, max_id + 1,
                 2**70, -(2**70), BIGINT_MAX, BIGINT_MIN, f"'{rng.randint(1, max_id)}'", "NULL"]
            )  # fmt: skip
            conds.append(f"{v} {_flip(op)} id" if rng.random() < 0.2 else f"id {op} {v}")
        elif kind < 0.8:
            v = rng.choice([rng.randint(-1, 101), "NULL", f"'{rng.randint(0, 100)}'"])
            conds.append(f"age {op} {v}")
        else:
            v = rng.choice(["'u1'", "'u100'", "'u5'", "''", "'한글'", "NULL", f"'u{rng.randint(0, 999)}'"])
            conds.append(f"name {op} {v}")
    return conds


def _flip(op):
    return {"=": "=", "<>": "<>", "<": ">", "<=": ">=", ">": "<", ">=": "<="}[op]


def test_differential_index_on_off(tmp_path):
    """같은 쿼리를 인덱스 켬/끔으로 실행한 결과 행이 정확히 같아야 한다.

    잡아내는 버그: 범위 병합의 포함/미포함 실수, 경계값(off-by-one), BIGINT 범위 밖 상수 처리,
    점 검색과 범위의 혼동, 리프 경계에서 범위가 끊기거나 중복되는 문제, 인덱스와 파일의 불일치,
    LIMIT 과 인덱스 순서의 상호작용.
    """
    rng = random.Random(20261113)
    # 열 때 벌크 로딩된 부분 + 열린 뒤 증분 삽입된 부분이 섞이도록 두 번에 나눠 넣는다
    db = make_db(tmp_path, index_order=4)  # 작은 order → 리프 경계가 많다
    names = [None, "u1", "u100", "u5", "", "한글"] + [f"u{i}" for i in range(1000)]
    rows = [(rng.choice(names), rng.choice([None, *range(0, 101)])) for _ in range(400)]
    insert_rows(db, rows[:250], batch=37)
    db.close()
    db = Database(tmp_path, index_order=4)
    insert_rows(db, rows[250:], batch=23)
    max_id = 400
    checked = 0
    kinds = Counter()
    with db:
        for _ in range(2500):
            conds = _random_where(rng, max_id)
            sql = "SELECT * FROM users"
            if conds:
                sql += " WHERE " + " AND ".join(conds)
            if rng.random() < 0.3:
                sql += f" LIMIT {rng.choice([0, 1, 3, 10, 100])}"
            on = db.execute_one(sql, ON).fetchall()
            off = db.execute_one(sql, OFF).fetchall()
            assert on == off, sql
            bound = bind(parse_statement(sql), db.catalog)
            node = choose_access_path(bound, db.table_handle(bound.table), ON)
            kinds[(type(node).__name__, getattr(node, "point", None) is not None, getattr(node, "empty", False))] += 1
            checked += 1
        db.table_handle(db.catalog.get("users")).index.check_invariants()
    assert checked == 2500
    # 점 검색, 범위 검색, 모순(empty), Seq Scan 이 모두 충분히 섞였는지
    assert kinds[("IndexScan", True, False)] > 100
    assert kinds[("IndexScan", False, False)] > 300
    assert kinds[("IndexScan", False, True)] > 50
    assert kinds[("SeqScan", False, False)] > 300


# ------------------------------------------------------------------ 재시작 / 복구 / 스캔 횟수


def index_matches_file(handle):
    file_entries = [(row[0], off) for off, row in handle.file.scan()]
    assert list(handle.index.items()) == file_entries
    handle.index.check_invariants()
    return file_entries


def test_index_rebuilt_on_restart(tmp_path):
    db = make_db(tmp_path, rows=1234)
    h = db.table_handle(db.catalog.get("users"))
    before = list(h.index.items())
    db.close()
    with Database(tmp_path) as db2:
        h2 = db2.table_handle(db2.catalog.get("users"))
        assert index_matches_file(h2) == before
        assert db2.execute_one("SELECT name FROM users WHERE id = 1234").fetchall() == [("u1233",)]


def test_index_after_torn_tail_recovery(tmp_path):
    db = make_db(tmp_path, rows=100)
    h = db.table_handle(db.catalog.get("users"))
    last_off = h.index.search(100)
    path = h.path
    db.close()
    raw = path.read_bytes()
    path.write_bytes(raw[: last_off + RECORD_HEADER_SIZE + 3])  # 마지막 레코드를 찢는다
    with pytest.warns(RuntimeWarning, match="truncated"):
        db2 = Database(tmp_path)
        h2 = db2.table_handle(db2.catalog.get("users"))
    with db2:
        entries = index_matches_file(h2)
        assert [k for k, _ in entries] == list(range(1, 100))
        assert ids(db2, "SELECT * FROM users WHERE id = 100") == []
        assert ids(db2, "SELECT * FROM users WHERE id >= 98") == [98, 99]
        # 복구 후 새 행은 잘린 자리에 id 100 으로 들어가고 인덱스도 따라간다
        db2.execute_one("INSERT INTO users (name) VALUES ('again')")
        assert db2.execute_one("SELECT name FROM users WHERE id = 100").fetchall() == [("again",)]
        index_matches_file(h2)


def test_index_skips_deleted_records(tmp_path):
    import struct
    import zlib

    db = make_db(tmp_path, rows=5)
    h = db.table_handle(db.catalog.get("users"))
    off = h.index.search(3)
    path = h.path
    db.close()
    raw = bytearray(path.read_bytes())
    (length,) = struct.unpack_from("<I", raw, off)
    body = bytearray(raw[off + RECORD_HEADER_SIZE : off + RECORD_HEADER_SIZE + length])
    body[0] |= storage_mod.FLAG_DELETED
    len_crc = struct.pack("<II", length, zlib.crc32(body))
    raw[off : off + RECORD_HEADER_SIZE + length] = len_crc + struct.pack("<I", zlib.crc32(len_crc)) + body
    path.write_bytes(bytes(raw))
    with Database(tmp_path) as db2:
        h2 = db2.table_handle(db2.catalog.get("users"))
        assert [k for k, _ in h2.index.items()] == [1, 2, 4, 5]
        assert ids(db2, "SELECT * FROM users WHERE id = 3") == []
        assert ids(db2, "SELECT * FROM users WHERE id >= 2", ON) == ids(db2, "SELECT * FROM users WHERE id >= 2", OFF)


def test_file_scanned_once_at_open(tmp_path, monkeypatch):
    make_db(tmp_path, rows=2000).close()
    calls = []
    real = storage_mod._iter_raw

    def counting(*args, **kw):
        calls.append(args[1:3])
        return real(*args, **kw)

    monkeypatch.setattr(storage_mod, "_iter_raw", counting)
    with Database(tmp_path) as db:
        h = db.table_handle(db.catalog.get("users"))
        assert len(calls) == 1  # 검증 스캔 한 번에 인덱스까지 구성
        assert calls[0][0] == storage_mod.HEADER_SIZE
        assert len(h.index) == 2000
        # 점 검색은 파일 스캔을 하지 않는다 (read_at 한 번)
        assert ids(db, "SELECT * FROM users WHERE id = 1500") == [1500]
        assert len(calls) == 1
        # 범위 검색은 트리에서 찾은 시작 위치부터만 순차로 읽는다 (파일 처음부터가 아님)
        assert ids(db, "SELECT * FROM users WHERE id >= 1990") == list(range(1990, 2001))
        assert len(calls) == 2 and calls[1][0] == h.index.search(1990)
        ids(db, "SELECT * FROM users WHERE age = 3")  # Seq Scan 은 처음부터 스캔한다
        assert len(calls) == 3 and calls[2][0] == storage_mod.HEADER_SIZE


# ------------------------------------------------------------------ 인덱스 무효화와 재구성


class _Boom(Exception):
    pass


def test_index_invalidated_and_rebuilt_on_unexpected_error(db, monkeypatch):
    h = db.table_handle(db.catalog.get("users"))
    real_insert = BPlusTree.insert
    calls = {"n": 0}

    def flaky(self, key, value):
        calls["n"] += 1
        if calls["n"] == 2:
            raise _Boom("index failure")
        return real_insert(self, key, value)

    monkeypatch.setattr(BPlusTree, "insert", flaky)
    r = db.execute_one("INSERT INTO users (name) VALUES ('a'), ('b'), ('c')")
    assert r.command_tag == "INSERT 0 3"  # 파일에는 썼으므로 INSERT 는 성공
    assert not h.index_valid
    monkeypatch.setattr(BPlusTree, "insert", real_insert)
    with pytest.warns(RuntimeWarning, match=r'index "users_pkey" was invalidated .* rebuilt .*\(303 entries\)'):
        assert ids(db, "SELECT * FROM users WHERE id = 302") == [302]
    assert h.index_valid and h.rebuilds == 1
    index_matches_file(h)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert ids(db, "SELECT * FROM users WHERE id = 303") == [303]


def test_duplicate_key_becomes_unique_violation(db):
    h = db.table_handle(db.catalog.get("users"))
    # 비정상 상황 재현: 파일의 next_id 를 되돌려 이미 있는 id 를 다시 할당하게 만든다
    h.file._next_id = 300
    with pytest.raises(UniqueViolation) as ei:
        db.execute_one("INSERT INTO users (name) VALUES ('dup')")
    e = ei.value
    assert e.sqlstate == "23505"
    assert e.message == 'duplicate key value violates unique constraint "users_pkey"'
    assert e.detail == "Key (id)=(300) already exists."
    assert not h.index_valid  # 방어: 인덱스를 버리고 파일에서 다시 구성한다


def test_index_name_for():
    assert index_name_for("users") == "users_pkey"
    assert index_name_for("Users") == "Users_pkey"
    long = "a" * 63
    assert index_name_for(long) == "a" * 58 + "_pkey"
    kor = "가" * 21  # 63 바이트
    name = index_name_for(kor)
    assert len(name.encode()) <= 63 and name == "가" * 19 + "_pkey"


# ------------------------------------------------------------------ EXPLAIN


def test_explain_index_scan_shapes(db):
    assert plan_text(db, "SELECT * FROM users WHERE id = 42") == [
        "Index Scan using users_pkey on users",
        "  Index Cond: (id = 42)",
    ]
    assert plan_text(db, "SELECT * FROM users WHERE id >= 1000 AND id < 2000") == [
        "Index Scan using users_pkey on users",
        "  Index Cond: ((id >= 1000) AND (id < 2000))",
        "  Heap Fetch: sequential",
    ]
    assert plan_text(db, "SELECT name FROM users WHERE id < 10 AND age > 3 AND id <> 4 LIMIT 2") == [
        "Limit",
        "  ->  Index Scan using users_pkey on users",
        "        Index Cond: (id < 10)",
        "        Heap Fetch: sequential",
        "        Filter: ((age > 3) AND (id <> 4))",
    ]
    assert plan_text(db, "SELECT * FROM users WHERE id = '7'") == [
        "Index Scan using users_pkey on users",
        "  Index Cond: (id = '7'::bigint)",
    ]
    assert plan_text(db, "SELECT * FROM users WHERE id = 42", OFF) == [
        "Seq Scan on users",
        "  Filter: (id = 42)",
    ]


def test_explain_analyze_index_scan(db):
    lines = plan_text(db, "ANALYZE SELECT * FROM users WHERE id > 10 AND id <= 20 AND age > 15")
    assert lines[0].startswith("Index Scan using users_pkey on users  (actual time=")
    # id 11..20 의 age 는 10..19 → age > 15 는 4행 통과, 6행 제거
    assert lines[0].endswith("rows=4 loops=1)")
    assert lines[1:5] == [
        "  Index Cond: ((id > 10) AND (id <= 20))",
        "  Heap Fetch: sequential",
        "  Filter: (age > 15)",
        "  Rows Removed by Filter: 6",
    ]
    assert lines[5].startswith("Execution Time: ")
    lines = plan_text(db, "ANALYZE SELECT * FROM users WHERE id = 3")
    assert lines[0].endswith("rows=1 loops=1)") and lines[1] == "  Index Cond: (id = 3)"
    assert not any("Heap Fetch" in x for x in lines)  # 점 검색은 read_at 한 번
    assert not any("Rows Removed" in x for x in lines)


def test_explain_quoted_and_korean_tables(tmp_path):
    (tmp_path / "schema").mkdir()
    for oid, name in ((1, "Users"), (2, "회원")):
        cols = [{"name": "id", "type": "BIGINT", "auto_increment": True}, {"name": "v", "type": "TEXT"}]
        doc = {"oid": oid, "table": name, "columns": cols}
        (tmp_path / "schema" / f"{oid}.json").write_text(json.dumps(doc), encoding="utf-8")
    with Database(tmp_path) as db:
        assert plan_text(db, 'SELECT * FROM "Users" WHERE id = 1')[0] == 'Index Scan using "Users_pkey" on "Users"'
        assert plan_text(db, "SELECT * FROM 회원 WHERE id = 1")[0] == "Index Scan using 회원_pkey on 회원"


def test_table_handle_open_standalone(tmp_path):
    db = make_db(tmp_path, rows=10)
    path = db.catalog.data_path("users")
    schema = db.catalog.get("users")
    db.close()
    h = TableHandle.open(path, schema, order=5)
    try:
        assert h.index.order == 5 and len(h.index) == 10
        scan = IndexScan(h, schema, index_conds=(), point=4)
        assert [r[0] for r in scan] == [4]
        assert explain_lines(scan) == ["Index Scan using users_pkey on users"]
    finally:
        h.close()


# ------------------------------------------------------------------ 클러스터드 범위 스캔


@pytest.mark.parametrize(
    ("where", "expected"),
    [
        ("id >= 10 AND id <= 12", [10, 11, 12]),
        ("id > 10 AND id <= 12", [11, 12]),
        ("id >= 10 AND id < 12", [10, 11]),
        ("id > 10 AND id < 12", [11]),
        ("id > 10 AND id < 11", []),
        ("id >= 299", [299, 300]),
        ("id > 300", []),
        ("id < 1", []),
        ("id <= 1", [1]),
        ("id > 0 AND id < 3", [1, 2]),
    ],
)
def test_range_scan_bounds_without_read_at(db, monkeypatch, where, expected):
    monkeypatch.setattr(TableFile, "read_at", lambda *a: pytest.fail("range scan must not use read_at"))
    node = access_path(db, where)
    assert isinstance(node, IndexScan) and node.point is None
    assert ids(db, f"SELECT * FROM users WHERE {where}") == expected
    monkeypatch.undo()
    assert ids(db, f"SELECT * FROM users WHERE {where}", OFF) == expected


def test_range_scan_stops_at_upper_bound(db, monkeypatch):
    seen = []
    real = TableFile.scan_from

    def spy(self, start, end=None):
        for item in real(self, start, end):
            seen.append(item[1][0])
            yield item

    monkeypatch.setattr(TableFile, "scan_from", spy)
    assert ids(db, "SELECT * FROM users WHERE id >= 100 AND id < 105") == [100, 101, 102, 103, 104]
    assert seen == [100, 101, 102, 103, 104, 105]  # 상한을 넘은 첫 행(105)에서 멈춘다
