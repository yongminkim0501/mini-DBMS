import json

import pytest

from minidb.binder import bind
from minidb.catalog import Catalog
from minidb.errors import ProgramLimitExceeded
from minidb.executor import Filter, Limit, Project, SeqScan, explain_lines
from minidb.planner import choose_access_path, plan_insert, plan_select
from minidb.sql.parser import parse_statement
from minidb.table import TableHandle

ROWS = [
    ("kim", 25),
    ("lee", 31),
    ("park", None),
    (None, 40),
    ("choi", 31),
    ("한글", 7),
]


@pytest.fixture
def env(tmp_path):
    (tmp_path / "schema").mkdir()
    users = {
        "oid": 1,
        "table": "users",
        "columns": [
            {"name": "id", "type": "BIGINT", "auto_increment": True},
            {"name": "name", "type": "TEXT"},
            {"name": "age", "type": "BIGINT"},
        ],
    }
    (tmp_path / "schema" / "1.json").write_text(json.dumps(users), encoding="utf-8")
    cat = Catalog(tmp_path)
    files: dict[int, TableHandle] = {}

    def tables(schema):
        if schema.oid not in files:
            files[schema.oid] = TableHandle.open(cat.data_path(schema.name), schema, max_text_bytes=100)
        return files[schema.oid]

    tables(cat.get("users")).append_many(ROWS)
    yield cat, tables
    for f in files.values():
        f.close()


def run(env, sql):
    cat, tables = env
    plan = plan_select(bind(parse_statement(sql), cat), tables)
    return plan, list(plan)


def names(rows):
    return [r[0] for r in rows]


# ------------------------------------------------------------------ 비교 연산자


@pytest.mark.parametrize(
    "where, ids",
    [
        ("age = 31", [2, 5]),
        ("age <> 31", [1, 4, 6]),  # NULL 인 park(3) 은 UNKNOWN → 제외
        ("age != 31", [1, 4, 6]),
        ("age < 31", [1, 6]),
        ("age <= 31", [1, 2, 5, 6]),
        ("age > 31", [4]),
        ("age >= 31", [2, 4, 5]),
        ("31 < age", [4]),  # 리터럴이 왼쪽
        ("age = '31'", [2, 5]),  # 문자열 → bigint
        ("name = 'kim'", [1]),
        ("name < 'l'", [1, 5]),  # 코드포인트 순서: 'choi' < 'kim' < 'l' < 'lee'
        ("name > 'z'", [6]),  # 한글은 ASCII 보다 큰 코드포인트
        ("id >= 5", [5, 6]),
    ],
)
def test_comparison_operators(env, where, ids):
    _, rows = run(env, f"SELECT id FROM users WHERE {where}")
    assert names(rows) == ids


def test_and_combination(env):
    _, rows = run(env, "SELECT id FROM users WHERE age >= 25 AND age < 40 AND name <> 'lee'")
    assert names(rows) == [1, 5]
    _, rows = run(env, "SELECT id FROM users WHERE age = 31 AND name = 'nobody'")
    assert rows == []


@pytest.mark.parametrize(
    "where",
    ["age = NULL", "age <> NULL", "NULL = age", "name = NULL", "name <> NULL", "age > 0 AND age = NULL"],
)
def test_null_comparison_never_passes(env, where):
    plan, rows = run(env, f"SELECT * FROM users WHERE {where}")
    assert rows == []


def test_null_column_values_are_unknown(env):
    _, rows = run(env, "SELECT id FROM users WHERE name <> 'x'")
    assert 4 not in names(rows)  # name 이 NULL 인 행은 어떤 비교도 통과하지 못한다
    _, rows = run(env, "SELECT id FROM users WHERE age >= 0")
    assert 3 not in names(rows)


# ------------------------------------------------------------------ Project / LIMIT


def test_projection_order_and_duplicates(env):
    _, rows = run(env, "SELECT age, name, age FROM users LIMIT 2")
    assert rows == [(25, "kim", 25), (31, "lee", 31)]
    _, rows = run(env, "SELECT * FROM users LIMIT 1")
    assert rows == [(1, "kim", 25)]


def test_limit_zero_and_all(env):
    plan, rows = run(env, "SELECT * FROM users LIMIT 0")
    assert rows == []
    scan = plan.child.child
    assert isinstance(scan, SeqScan) and scan.rows_scanned == 0  # 하위 노드를 시작조차 하지 않음
    _, rows = run(env, "SELECT * FROM users LIMIT ALL")
    assert len(rows) == len(ROWS)
    _, rows = run(env, "SELECT * FROM users LIMIT 100")
    assert len(rows) == len(ROWS)


def test_limit_stops_scan_early(tmp_path):
    (tmp_path / "schema").mkdir()
    doc = {
        "oid": 9,
        "table": "big",
        "columns": [{"name": "id", "type": "BIGINT", "auto_increment": True}, {"name": "v", "type": "BIGINT"}],
    }
    (tmp_path / "schema" / "9.json").write_text(json.dumps(doc))
    cat = Catalog(tmp_path)
    schema = cat.get("big")
    tf = TableHandle.open(cat.data_path("big"), schema)
    try:
        tf.append_many([(i % 10,) for i in range(20_000)])

        def tables(_s):
            return tf

        plan = plan_select(bind(parse_statement("SELECT * FROM big LIMIT 10"), cat), tables)
        assert isinstance(plan, Limit)
        assert len(list(plan)) == 10
        scan = plan.child.child
        assert scan.rows_scanned == 10  # 정확히 10개만 읽고 멈춘다

        plan = plan_select(bind(parse_statement("SELECT * FROM big WHERE v = 9 LIMIT 3"), cat), tables)
        assert [r[0] for r in plan] == [10, 20, 30]
        assert plan.child.child.child.rows_scanned == 30  # 조건을 통과한 3번째 행(id 30)에서 멈춤

        # 이터레이터를 일부만 읽어도 그만큼만 스캔한다
        plan = plan_select(bind(parse_statement("SELECT * FROM big"), cat), tables)
        it = iter(plan)
        for _ in range(5):
            next(it)
        assert plan.child.rows_scanned == 5
    finally:
        tf.close()


def test_plan_shape(env):
    plan, _ = run(env, "SELECT name FROM users WHERE age > 1 LIMIT 2")
    assert isinstance(plan, Limit)
    assert isinstance(plan.child, Project)
    assert isinstance(plan.child.child, Filter)
    assert isinstance(plan.child.child.child, SeqScan)
    plan, _ = run(env, "SELECT * FROM users")
    assert isinstance(plan, Project) and plan.identity


def test_choose_access_path_without_id_condition_is_seqscan(env):
    cat, tables = env
    bound = bind(parse_statement("SELECT * FROM users WHERE age = 1"), cat)
    assert isinstance(choose_access_path(bound, tables(bound.table)), SeqScan)


# ------------------------------------------------------------------ INSERT


def test_insert_returning(env):
    cat, tables = env
    node = plan_insert(bind(parse_statement("INSERT INTO users (age, name) VALUES (1, 'a'), (2, 'b') RETURNING id, name"), cat), tables)
    assert list(node) == [(7, "a"), (8, "b")]
    assert node.inserted == 2
    node = plan_insert(bind(parse_statement("INSERT INTO users (name) VALUES ('c')"), cat), tables)
    assert list(node) == [] and node.inserted == 1


def test_multi_row_insert_is_atomic(env):
    cat, tables = env
    tf = tables(cat.get("users")).file
    size, next_id = tf.size, tf.next_id
    # 바인딩은 통과하지만 저장 계층 검증(max_text_bytes=100)에서 세 번째 행이 실패
    sql = "INSERT INTO users (name) VALUES ('ok1'), ('ok2'), ('" + "x" * 101 + "')"
    node = plan_insert(bind(parse_statement(sql), cat), tables)
    with pytest.raises(ProgramLimitExceeded):
        list(node)
    assert (tf.size, tf.next_id) == (size, next_id)
    _, rows = run(env, "SELECT id FROM users WHERE id > 6")
    assert rows == []


# ------------------------------------------------------------------ EXPLAIN 형식


def test_explain_lines_format(env):
    plan, _ = run(env, "SELECT name FROM users WHERE age > 30 AND name = 'kim' LIMIT 3")
    assert explain_lines(plan) == [
        "Limit",
        "  ->  Seq Scan on users",
        "        Filter: ((age > 30) AND (name = 'kim'::text))",
    ]
    plan, _ = run(env, "SELECT * FROM users WHERE age = '25' AND age <> NULL")
    assert explain_lines(plan) == ["Seq Scan on users", "  Filter: ((age = '25'::bigint) AND (age <> NULL::bigint))"]
    plan, _ = run(env, "SELECT * FROM users")
    assert explain_lines(plan) == ["Seq Scan on users"]
    plan, _ = run(env, "SELECT * FROM users WHERE 5 < age")
    assert explain_lines(plan) == ["Seq Scan on users", "  Filter: (age > 5)"]
