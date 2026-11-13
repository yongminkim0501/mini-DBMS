"""파라미터 ($n): 파서, 타입 추론, 지정 타입 OID, 값 채우기, 플래너(Index Scan), 단순 쿼리/CLI 의 42P02."""

import io
import json

import pytest

from minidb.__main__ import main
from minidb.binder import ParamContext, ParamRef, bind, bind_values
from minidb.catalog import Catalog
from minidb.engine import Database
from minidb.errors import (
    AmbiguousParameter,
    FeatureNotSupported,
    IndeterminateDatatype,
    InvalidRowCountInLimitClause,
    InvalidTextRepresentation,
    SqlSyntaxError,
    UndefinedFunction,
    UndefinedParameter,
)
from minidb.sql.ast import Condition, InsertStmt, Param, SelectStmt
from minidb.sql.parser import parse_statement

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


@pytest.fixture
def db(db_dir):
    with Database(db_dir) as d:
        d.execute_one("INSERT INTO users (name, age) VALUES ('kim', 25), ('lee', 31), ('park', NULL)")
        yield d


def prepare_types(db, sql, oids=()):
    return db.prepare(sql, oids).param_types


# ------------------------------------------------------------------ 파서


def test_parser_accepts_params_in_literal_positions():
    s = parse_statement("SELECT * FROM users WHERE id = $1 AND $2 < age LIMIT $3")
    assert isinstance(s, SelectStmt)
    assert s.where[0] == Condition("id", "=", Param(1))
    assert s.where[1] == Condition("age", ">", Param(2))  # $2 < age → age > $2 (정규화)
    assert s.limit == Param(3)
    assert s.to_sql() == "SELECT * FROM users WHERE id = $1 AND age > $2 LIMIT $3"
    i = parse_statement("INSERT INTO users (name, age) VALUES ($1, $2), ('x', DEFAULT)")
    assert isinstance(i, InsertStmt) and i.rows[0] == (Param(1), Param(2))
    assert parse_statement(i.to_sql()) == i


@pytest.mark.parametrize(
    "sql",
    ["SELECT $1 FROM users", "SET enable_indexscan = $1", "SELECT * FROM users WHERE id = -$1", "SELECT * FROM $1"],
)
def test_parser_rejects_params_elsewhere(sql):
    with pytest.raises(SqlSyntaxError):
        parse_statement(sql)


# ------------------------------------------------------------------ 타입 추론


def test_inference_by_position(db):
    assert prepare_types(db, "INSERT INTO users (name, age) VALUES ($1, $2)") == (25, 20)
    assert prepare_types(db, "INSERT INTO users (age, name) VALUES ($1, $2), ($1, 'x')") == (20, 25)
    assert prepare_types(db, "SELECT * FROM users WHERE name = $1 AND id > $2") == (25, 20)
    assert prepare_types(db, "SELECT * FROM users LIMIT $1") == (20,)
    assert prepare_types(db, "EXPLAIN SELECT * FROM users WHERE $1 = id") == (20,)
    assert prepare_types(db, "SELECT * FROM users") == ()


def test_declared_types(db):
    assert prepare_types(db, "SELECT * FROM users WHERE id = $1", (23,)) == (23,)
    assert prepare_types(db, "SELECT * FROM users WHERE id = $1", (0,)) == (20,)
    assert prepare_types(db, "SELECT * FROM users WHERE id = $1", (705,)) == (20,)  # unknown → 자리에서 추론
    assert prepare_types(db, "SELECT * FROM users WHERE id = $1", (25,)) == (25,)  # 문자열 → bigint 변환 규칙
    assert prepare_types(db, "SELECT * FROM users WHERE name = $1", (1043,)) == (1043,)
    # 쓰이지 않았어도 타입이 지정된 파라미터는 개수에 들어간다
    assert prepare_types(db, "SELECT * FROM users WHERE id = $1", (20, 25)) == (20, 25)


def test_indeterminate_and_inconsistent(db):
    with pytest.raises(IndeterminateDatatype) as ei:
        db.prepare("SELECT * FROM users WHERE id = $2")
    assert (ei.value.sqlstate, ei.value.message) == ("42P18", "could not determine data type of parameter $1")
    with pytest.raises(IndeterminateDatatype):
        db.prepare("SELECT * FROM users", (0,))
    with pytest.raises(AmbiguousParameter) as ei:
        db.prepare("SELECT * FROM users WHERE id = $1 AND name = $1")
    assert ei.value.detail == "bigint versus text"
    with pytest.raises(FeatureNotSupported):
        db.prepare("SELECT * FROM users WHERE id = $1", (16,))


def test_declared_integer_vs_text_column(db):
    sql = "SELECT * FROM users WHERE name = $1"
    with pytest.raises(UndefinedFunction) as ei:
        db.prepare(sql, (23,))
    assert ei.value.message == "operator does not exist: text = integer"
    assert ei.value.position == sql.index("=") + 1
    with pytest.raises(UndefinedFunction) as ei:
        db.prepare("SELECT * FROM users WHERE $1 < name", (20,))
    assert ei.value.message == "operator does not exist: bigint < text"


# ------------------------------------------------------------------ 값 채우기와 실행


def test_execute_prepared_reuses_template(db):
    p = db.prepare("SELECT name FROM users WHERE id = $1")
    assert [db.execute_prepared(p, [i]).fetchall() for i in (1, 2, 3, 9)] == [[("kim",)], [("lee",)], [("park",)], []]


def test_text_param_into_bigint_uses_literal_rules(db):
    p = db.prepare("SELECT name FROM users WHERE age = $1", (25,))
    assert db.execute_prepared(p, [" 31 "]).fetchall() == [("lee",)]
    with pytest.raises(InvalidTextRepresentation) as ei:
        db.execute_prepared(p, ["x"])
    assert ei.value.message == 'invalid input syntax for type bigint: "x"'
    ins = db.prepare("INSERT INTO users (name, age) VALUES ($1, $2) RETURNING name, age", (23, 25))
    assert db.execute_prepared(ins, [7, "8"]).fetchall() == [("7", 8)]  # int → text 대입, 문자열 → bigint


def test_null_params(db):
    p = db.prepare("SELECT * FROM users WHERE age = $1")
    assert db.execute_prepared(p, [None]).fetchall() == []
    ins = db.prepare("INSERT INTO users (name, age) VALUES ($1, $2) RETURNING *")
    assert db.execute_prepared(ins, [None, None]).fetchall() == [(4, None, None)]


def test_limit_param(db):
    p = db.prepare("SELECT id FROM users LIMIT $1")
    assert db.execute_prepared(p, [2]).fetchall() == [(1,), (2,)]
    assert db.execute_prepared(p, [None]).fetchall() == [(1,), (2,), (3,)]
    with pytest.raises(InvalidRowCountInLimitClause):
        db.execute_prepared(p, [-1])


def test_bind_values_keeps_template_untouched(db):
    cat: Catalog = db.catalog
    ctx = ParamContext()
    tmpl = bind(parse_statement("SELECT * FROM users WHERE id = $1"), cat, ctx)
    assert isinstance(tmpl.where[0].value, ParamRef)
    a = bind_values(tmpl, [1])
    b = bind_values(tmpl, [2])
    assert (a.where[0].value, b.where[0].value) == (1, 2)
    assert isinstance(tmpl.where[0].value, ParamRef)
    no_params = bind(parse_statement("SELECT * FROM users"), cat, ParamContext())
    assert bind_values(no_params, []) is no_params


def test_id_param_chooses_point_index_scan(db):
    p = db.prepare("EXPLAIN SELECT * FROM users WHERE id = $1")
    plan = [r[0] for r in db.execute_prepared(p, [2]).fetchall()]
    assert plan == ["Index Scan using users_pkey on users", "  Index Cond: (id = $1)"]
    p = db.prepare("EXPLAIN SELECT * FROM users WHERE id >= $1 AND name = $2")
    plan = [r[0] for r in db.execute_prepared(p, [2, "lee"]).fetchall()]
    assert plan[:2] == ["Index Scan using users_pkey on users", "  Index Cond: (id >= $1)"]
    assert plan[-1] == "  Filter: (name = $2)"


def test_prepare_errors(db):
    with pytest.raises(SqlSyntaxError) as ei:
        db.prepare("SELECT * FROM users; SELECT * FROM users")
    assert ei.value.message == "cannot insert multiple commands into a prepared statement"
    empty = db.prepare("  -- nothing\n")
    assert empty.bound is None and empty.columns is None


# ------------------------------------------------------------------ 단순 쿼리 / CLI


def test_simple_execution_rejects_params(db):
    sql = "SELECT * FROM users WHERE id = $1"
    with pytest.raises(UndefinedParameter) as ei:
        db.execute_one(sql)
    assert (ei.value.sqlstate, ei.value.message, ei.value.position) == ("42P02", "there is no parameter $1", 32)
    with pytest.raises(UndefinedParameter):
        db.execute_one("INSERT INTO users (name) VALUES ($1)")
    with pytest.raises(UndefinedParameter):
        db.execute_one("SELECT * FROM users LIMIT $1")


def test_cli_reports_undefined_parameter(db_dir):
    out, err = io.StringIO(), io.StringIO()
    code = main(["-c", "SELECT * FROM users WHERE id = $1", "--db", str(db_dir)], stdout=out, stderr=err)
    assert code == 1
    assert err.getvalue() == (
        "ERROR:  there is no parameter $1\n"
        "LINE 1: SELECT * FROM users WHERE id = $1\n"
        "                                       ^\n"
    )
