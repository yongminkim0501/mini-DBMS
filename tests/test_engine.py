import json
import re

import pytest

from minidb.engine import Database
from minidb.errors import DatabaseError, NotNullViolation, SqlSyntaxError, UndefinedTable
from minidb.result import QueryResult


def _write(db_dir, oid, name, cols):
    doc = {
        "oid": oid,
        "table": name,
        "columns": [{"name": "id", "type": "BIGINT", "auto_increment": True}, *cols],
    }
    (db_dir / "schema" / f"{oid}.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def db_dir(tmp_path):
    (tmp_path / "schema").mkdir()
    _write(tmp_path, 16384, "users", [{"name": "name", "type": "TEXT"}, {"name": "age", "type": "BIGINT"}])
    _write(tmp_path, 16385, "Users", [{"name": "tag", "type": "TEXT", "nullable": False}])
    return tmp_path


@pytest.fixture
def db(db_dir):
    with Database(db_dir) as d:
        yield d


def results(db, sql):
    out = []
    for r in db.execute(sql):
        if isinstance(r, DatabaseError):
            out.append(r)
        else:
            out.append((None if r.columns is None else [c.name for c in r.columns], list(r.rows), r.command_tag))
    return out


def test_basic_pipeline(db):
    got = results(db, "INSERT INTO users (name, age) VALUES ('kim', 25), ('lee', 31); SELECT * FROM users")
    assert got == [
        (None, [], "INSERT 0 2"),
        (["id", "name", "age"], [(1, "kim", 25), (2, "lee", 31)], "SELECT 2"),
    ]


def test_returning_and_tags(db):
    (r,) = results(db, "INSERT INTO users VALUES (DEFAULT, 'a', 1), (DEFAULT, 'b', DEFAULT) RETURNING id, age")
    assert r == (["id", "age"], [(1, 1), (2, None)], "INSERT 0 2")
    (r,) = results(db, "SELECT name FROM users WHERE id = 99")
    assert r == (["name"], [], "SELECT 0")


def test_select_is_streamed(db):
    db.execute_one("INSERT INTO users (name) VALUES ('a'), ('b'), ('c')")
    res = db.execute_one("SELECT name FROM users")
    assert not isinstance(res.rows, list)  # 이터레이터
    assert next(iter(res)) == ("a",)
    assert res.command_tag == "SELECT 3"  # 태그를 읽으면 남은 행을 소비한다


def test_column_infos(db):
    res = db.execute_one("SELECT age, name FROM users")
    assert [(c.name, c.type_name, c.type_oid) for c in res.columns] == [("age", "bigint", 20), ("name", "text", 25)]


def test_errors_are_yielded_and_execution_continues(db):
    sql = "INSERT INTO users (name) VALUES ('ok');\nSELECT * FORM users;\nSELECT * FROM nope;\nSELECT name FROM users"
    got = results(db, sql)
    assert got[0] == (None, [], "INSERT 0 1")
    assert isinstance(got[1], SqlSyntaxError) and got[1].position == sql.index("FORM") + 1
    assert isinstance(got[2], UndefinedTable) and got[2].position == sql.index("nope") + 1  # 스크립트 기준
    assert got[3] == (["name"], [("ok",)], "SELECT 1")


def test_lex_error_stops_splitting(db):
    got = results(db, "INSERT INTO users (name) VALUES ('a'); SELECT 'unterminated; INSERT INTO users (age) VALUES (1)")
    assert got[0][2] == "INSERT 0 1"
    assert isinstance(got[1], SqlSyntaxError) and got[1].message == "unterminated quoted string"
    assert len(got) == 2


def test_failed_insert_writes_nothing(db):
    db.execute_one("INSERT INTO \"Users\" (tag) VALUES ('t1')")
    with pytest.raises(NotNullViolation):
        db.execute_one("INSERT INTO \"Users\" (tag) VALUES ('t2'), (NULL)")
    assert db.execute_one('SELECT tag FROM "Users"').fetchall() == [("t1",)]


def test_quoted_and_unquoted_tables_end_to_end(db, db_dir):
    db.execute_one("INSERT INTO users (name) VALUES ('lower')")
    db.execute_one("INSERT INTO \"Users\" (tag) VALUES ('UPPER')")
    db.execute_one("INSERT INTO USERS (name) VALUES ('lower2')")  # 비인용 USERS → users
    assert db.execute_one("SELECT name FROM Users").fetchall() == [("lower",), ("lower2",)]
    assert db.execute_one('SELECT tag FROM "Users"').fetchall() == [("UPPER",)]
    with pytest.raises(UndefinedTable):
        db.execute_one('SELECT * FROM "USERS"')
    db.close()
    assert sorted(p.name for p in (db_dir / "data").iterdir()) == ["16384.tbl", "16385.tbl"]
    with Database(db_dir) as again:  # 다시 열어도 각자의 파일에서 읽는다
        assert again.execute_one('SELECT tag FROM "Users"').fetchall() == [("UPPER",)]
        assert len(again.execute_one("SELECT * FROM users").fetchall()) == 2


def test_catalog_loaded_eagerly(db_dir):
    (db_dir / "schema" / "bad.json").write_text("{broken")
    with pytest.raises(DatabaseError, match="bad.json"):
        Database(db_dir)  # 생성 시점에 바로 실패 (지연 로드 아님)


def test_table_files_cached_and_closed(db):
    db.execute_one("INSERT INTO users (name) VALUES ('a')")
    s = db.catalog.get("users")
    tf = db.table_file(s)
    assert db.table_file(s) is tf
    db.close()
    assert tf.closed
    with pytest.raises(ValueError):
        db.table_file(s)


def test_data_persists_across_reopen(db_dir):
    with Database(db_dir) as d:
        d.execute_one("INSERT INTO users (name, age) VALUES ('p', 1)")
    with Database(db_dir) as d:
        assert d.execute_one("INSERT INTO users (name) VALUES ('q') RETURNING id").fetchall() == [(2,)]


# ------------------------------------------------------------------ EXPLAIN


def test_explain_does_not_execute(db):
    (r,) = results(db, "EXPLAIN INSERT INTO users (name) VALUES ('x'), ('y')")
    assert r == (["QUERY PLAN"], [("Insert on users",), ('  ->  Values Scan on "*VALUES*"',)], "EXPLAIN")
    assert db.execute_one("SELECT * FROM users").fetchall() == []
    (r,) = results(db, "EXPLAIN INSERT INTO users (name) VALUES ('x')")
    assert r[1] == [("Insert on users",), ("  ->  Result",)]


def test_explain_analyze_select_actual_rows(db):
    db.execute_one("INSERT INTO users (name, age) VALUES ('a', 10), ('b', 40), ('c', 50), ('d', 5)")
    res = db.execute_one("EXPLAIN ANALYZE SELECT name FROM users WHERE age > 30 LIMIT 1")
    lines = [r[0] for r in res.rows]
    assert re.fullmatch(r"Limit  \(actual time=\d+\.\d{3}\.\.\d+\.\d{3} rows=1 loops=1\)", lines[0])
    assert re.fullmatch(r"  ->  Seq Scan on users  \(actual time=[\d.]+\.\.[\d.]+ rows=1 loops=1\)", lines[1])
    assert lines[2] == "        Filter: (age > 30)"
    assert lines[3] == "        Rows Removed by Filter: 1"  # a(10) 를 버리고 b(40) 에서 LIMIT 이 멈춤
    assert re.fullmatch(r"Execution Time: \d+\.\d{3} ms", lines[4])
    assert res.command_tag == "EXPLAIN"

    res = db.execute_one("EXPLAIN ANALYZE SELECT * FROM users")
    assert "rows=4 loops=1" in res.fetchall()[0][0]


def test_explain_analyze_insert_really_inserts(db):
    res = db.execute_one("EXPLAIN ANALYZE INSERT INTO users (name) VALUES ('x'), ('y'), ('z')")
    lines = [r[0] for r in res.rows]
    assert re.fullmatch(r"Insert on users  \(actual time=[\d.]+\.\.[\d.]+ rows=0 loops=1\)", lines[0])
    assert re.fullmatch(r'  ->  Values Scan on "\*VALUES\*"  \(actual time=[\d.]+\.\.[\d.]+ rows=3 loops=1\)', lines[1])
    assert db.execute_one("SELECT name FROM users").fetchall() == [("x",), ("y",), ("z",)]


def test_explain_analyze_returns_no_result_rows(db):
    db.execute_one("INSERT INTO users (name) VALUES ('a')")
    res = db.execute_one("EXPLAIN ANALYZE SELECT * FROM users")
    assert [c.name for c in res.columns] == ["QUERY PLAN"]
    assert all(isinstance(r[0], str) for r in res.rows)


def test_query_result_requires_tag():
    with pytest.raises(ValueError):
        QueryResult(None, [])
