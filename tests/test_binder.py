import json

import pytest

from minidb.binder import BoundExplain, BoundInsert, BoundSelect, bind
from minidb.catalog import Catalog
from minidb.errors import (
    DuplicateColumn,
    GeneratedAlways,
    InvalidSchemaName,
    InvalidTextRepresentation,
    NotNullViolation,
    NumericValueOutOfRange,
    SqlSyntaxError,
    UndefinedColumn,
    UndefinedFunction,
    UndefinedTable,
)
from minidb.result import BIGINT_OID, TEXT_OID, ColumnInfo
from minidb.sql.parser import parse_statement


def _table(oid, name, *cols):
    return {
        "oid": oid,
        "table": name,
        "columns": [{"name": "id", "type": "BIGINT", "auto_increment": True}, *cols],
    }


@pytest.fixture
def catalog(tmp_path):
    d = tmp_path / "schema"
    d.mkdir()
    tables = [
        _table(1, "users", {"name": "name", "type": "TEXT"}, {"name": "age", "type": "BIGINT"}),
        _table(2, "strict", {"name": "name", "type": "TEXT", "nullable": False}, {"name": "age", "type": "BIGINT"}),
        _table(3, "Users", {"name": "Name", "type": "TEXT"}),
    ]
    for t in tables:
        (d / f"{t['oid']}.json").write_text(json.dumps(t), encoding="utf-8")
    return Catalog(tmp_path)


def B(sql, catalog):
    return bind(parse_statement(sql), catalog)


def err(sql, catalog, exc):
    with pytest.raises(exc) as ei:
        B(sql, catalog)
    return ei.value


def at(sql, needle, nth=0):
    """sql 에서 needle 의 (nth 번째) 위치 (1부터)."""
    i = -1
    for _ in range(nth + 1):
        i = sql.index(needle, i + 1)
    return i + 1


# ------------------------------------------------------------------ 테이블


def test_public_prefix_and_other_schema(catalog):
    b = B("SELECT * FROM public.users", catalog)
    assert b.table.name == "users"
    sql = "SELECT * FROM other.users"
    e = err(sql, catalog, InvalidSchemaName)
    assert (e.sqlstate, e.message, e.position) == ("3F000", 'schema "other" does not exist', at(sql, "other"))


def test_undefined_table(catalog):
    sql = "SELECT * FROM nope"
    e = err(sql, catalog, UndefinedTable)
    assert (e.sqlstate, e.message, e.position) == ("42P01", 'relation "nope" does not exist', at(sql, "nope"))
    e = err("SELECT * FROM public.nope", catalog, UndefinedTable)
    assert e.message == 'relation "public.nope" does not exist'


def test_quoted_and_unquoted_table_names_are_distinct(catalog):
    assert B("SELECT * FROM users", catalog).table.oid == 1
    assert B("SELECT * FROM USERS", catalog).table.oid == 1  # 비인용은 접힌다
    assert B('SELECT * FROM "Users"', catalog).table.oid == 3
    e = err('SELECT * FROM "USERS"', catalog, UndefinedTable)
    assert e.message == 'relation "USERS" does not exist'


# ------------------------------------------------------------------ INSERT: 컬럼 목록


def test_insert_with_column_list(catalog):
    b = B("INSERT INTO users (age, name) VALUES (30, 'kim'), (NULL, DEFAULT)", catalog)
    assert isinstance(b, BoundInsert)
    assert b.rows == (("kim", 30), (None, None))  # data_columns 순서로 재배열
    assert b.returning is None


def test_insert_partial_column_list_defaults_to_null(catalog):
    assert B("INSERT INTO users (age) VALUES (1)", catalog).rows == ((None, 1),)


def test_insert_undefined_and_duplicate_column(catalog):
    sql = "INSERT INTO users (name, nope) VALUES ('a', 1)"
    e = err(sql, catalog, UndefinedColumn)
    assert (e.sqlstate, e.message, e.position) == (
        "42703", 'column "nope" of relation "users" does not exist', at(sql, "nope"),
    )  # fmt: skip
    sql = "INSERT INTO users (name, age, name) VALUES ('a', 1, 'b')"
    e = err(sql, catalog, DuplicateColumn)
    assert (e.sqlstate, e.message, e.position) == (
        "42701", 'column "name" specified more than once', at(sql, "name", 1),
    )  # fmt: skip


def test_more_target_columns_than_expressions(catalog):
    sql = "INSERT INTO users (name, age) VALUES ('a')"
    e = err(sql, catalog, SqlSyntaxError)
    assert (e.sqlstate, e.message, e.position) == (
        "42601", "INSERT has more target columns than expressions", at(sql, "age"),
    )  # fmt: skip


def test_more_expressions_than_target_columns_with_list(catalog):
    sql = "INSERT INTO users (name) VALUES ('a', 2)"
    e = err(sql, catalog, SqlSyntaxError)
    assert (e.message, e.position) == ("INSERT has more expressions than target columns", at(sql, "2"))


# ------------------------------------------------------------------ INSERT: 컬럼 목록 없음 (PostgreSQL 규칙)


def test_no_column_list_exact(catalog):
    assert B("INSERT INTO users VALUES (DEFAULT, 'kim', 25)", catalog).rows == (("kim", 25),)


def test_no_column_list_value_maps_to_id(catalog):
    # 'kim' 이 id 에 대응한다 → GENERATED ALWAYS 위반
    sql = "INSERT INTO users VALUES ('kim', 25)"
    e = err(sql, catalog, GeneratedAlways)
    assert e.sqlstate == "428C9"
    assert e.message == 'cannot insert a non-DEFAULT value into column "id"'
    assert e.detail == 'Column "id" is an identity column defined as GENERATED ALWAYS.'
    assert e.position == at(sql, "'kim'")


def test_no_column_list_too_many(catalog):
    sql = "INSERT INTO users VALUES (DEFAULT, 'kim', 25, 99)"
    e = err(sql, catalog, SqlSyntaxError)
    assert (e.message, e.position) == ("INSERT has more expressions than target columns", at(sql, "99"))


def test_no_column_list_too_few_uses_defaults(catalog):
    assert B("INSERT INTO users VALUES (DEFAULT)", catalog).rows == ((None, None),)
    assert B("INSERT INTO users VALUES (DEFAULT, 'a')", catalog).rows == (("a", None),)


# ------------------------------------------------------------------ INSERT: id


def test_id_default_allowed_and_values_rejected(catalog):
    assert B("INSERT INTO users (id, name) VALUES (DEFAULT, 'a')", catalog).rows == (("a", None),)
    for v in ("1", "NULL", "'1'"):
        sql = f"INSERT INTO users (id, name) VALUES ({v}, 'a')"
        e = err(sql, catalog, GeneratedAlways)
        assert e.position == at(sql, v)


# ------------------------------------------------------------------ INSERT: 타입 변환


@pytest.mark.parametrize(
    "literal, expected",
    [("25", 25), ("'25'", 25), ("' 25 '", 25), ("'\t-7\n'", -7), ("'+3'", 3), ("-9223372036854775808", -(2**63))],
)
def test_bigint_assignment(catalog, literal, expected):
    assert B(f"INSERT INTO users (age) VALUES ({literal})", catalog).rows == ((None, expected),)


@pytest.mark.parametrize("text", ["abc", "", " ", "1.5", "1_000", "１２", "0x10", "1 2", "--1"])
def test_bigint_invalid_text(catalog, text):
    lit = "'" + text + "'"
    sql = f"INSERT INTO users (age) VALUES ({lit})"
    e = err(sql, catalog, InvalidTextRepresentation)
    assert (e.sqlstate, e.message, e.position) == (
        "22P02", f'invalid input syntax for type bigint: "{text}"', at(sql, lit),
    )  # fmt: skip


def test_bigint_out_of_range(catalog):
    sql = "INSERT INTO users (age) VALUES (9223372036854775808)"
    e = err(sql, catalog, NumericValueOutOfRange)
    assert (e.sqlstate, e.message, e.position) == ("22003", "bigint out of range", at(sql, "9223"))
    e = err("INSERT INTO users (age) VALUES ('99999999999999999999')", catalog, NumericValueOutOfRange)
    assert e.message == 'value "99999999999999999999" is out of range for type bigint'


def test_text_assignment(catalog):
    assert B("INSERT INTO users (name) VALUES ('x'), (25), (-3)", catalog).rows == (
        ("x", None), ("25", None), ("-3", None),
    )  # fmt: skip


def test_not_null_violation(catalog):
    for sql, expected_pos in [
        ("INSERT INTO strict (name, age) VALUES (NULL, 1)", lambda s: at(s, "NULL")),
        ("INSERT INTO strict (name) VALUES (DEFAULT)", lambda s: at(s, "DEFAULT")),
        # 생략된 컬럼은 가리킬 값이 없으므로 그 행의 첫 값 위치
        ("INSERT INTO strict (age) VALUES (1)", lambda s: at(s, "(1)") + 1),
        ("INSERT INTO strict VALUES (DEFAULT)", lambda s: at(s, "DEFAULT")),
    ]:
        e = err(sql, catalog, NotNullViolation)
        assert e.sqlstate == "23502"
        assert e.message == 'null value in column "name" of relation "strict" violates not-null constraint'
        assert e.position == expected_pos(sql), sql


def test_not_null_violation_in_later_row_fails_whole_statement(catalog):
    sql = "INSERT INTO strict (name) VALUES ('a'), ('b'), (NULL)"
    e = err(sql, catalog, NotNullViolation)
    assert e.position == at(sql, "NULL")


def test_conversion_error_in_later_row(catalog):
    sql = "INSERT INTO users (age) VALUES (1), (2), ('x')"
    assert err(sql, catalog, InvalidTextRepresentation).position == at(sql, "'x'")


# ------------------------------------------------------------------ RETURNING


def test_returning(catalog):
    b = B("INSERT INTO users (name) VALUES ('a') RETURNING *", catalog)
    assert b.returning == (0, 1, 2)
    assert [c.name for c in b.returning_infos] == ["id", "name", "age"]
    b = B("INSERT INTO users (name) VALUES ('a') RETURNING age, id", catalog)
    assert b.returning == (2, 0)
    sql = "INSERT INTO users (name) VALUES ('a') RETURNING id, nope"
    e = err(sql, catalog, UndefinedColumn)
    assert (e.message, e.position) == ('column "nope" does not exist', at(sql, "nope"))


# ------------------------------------------------------------------ SELECT


def test_select_columns(catalog):
    b = B("SELECT age, name, age FROM users", catalog)
    assert isinstance(b, BoundSelect)
    assert b.columns == (2, 1, 2)  # 중복 선택 허용
    assert b.column_infos == (
        ColumnInfo("age", "bigint", BIGINT_OID), ColumnInfo("name", "text", TEXT_OID), ColumnInfo("age", "bigint", 20),
    )  # fmt: skip
    assert B("SELECT * FROM users", catalog).columns == (0, 1, 2)


def test_select_undefined_columns(catalog):
    for sql, name in [("SELECT nope FROM users", "nope"), ("SELECT * FROM users WHERE nope = 1", "nope")]:
        e = err(sql, catalog, UndefinedColumn)
        assert (e.sqlstate, e.message, e.position) == ("42703", f'column "{name}" does not exist', at(sql, name))
    # 인용 식별자는 대소문자가 맞아야 한다
    assert err('SELECT "NAME" FROM users', catalog, UndefinedColumn).message == 'column "NAME" does not exist'
    assert B('SELECT "Name" FROM "Users"', catalog).columns == (1,)


def test_where_bigint_conversions(catalog):
    b = B("SELECT * FROM users WHERE age = 25 AND age > '30' AND age < ' 7 ' AND id = NULL", catalog)
    assert [(c.column_index, c.op, c.value) for c in b.where] == [(2, "=", 25), (2, ">", 30), (2, "<", 7), (0, "=", None)]
    sql = "SELECT * FROM users WHERE age = 'abc'"
    e = err(sql, catalog, InvalidTextRepresentation)
    assert (e.message, e.position) == ('invalid input syntax for type bigint: "abc"', at(sql, "'abc'"))
    # bigint 범위 밖 정수와의 비교는 허용 (PostgreSQL: bigint vs numeric)
    assert B("SELECT * FROM users WHERE age < 99999999999999999999", catalog).where[0].value == 99999999999999999999


def test_where_text_vs_integer_rejected(catalog):
    sql = "SELECT * FROM users WHERE name = 1"
    e = err(sql, catalog, UndefinedFunction)
    assert e.sqlstate == "42883"
    assert e.message == "operator does not exist: text = integer"
    assert e.hint == (
        "No operator matches the given name and argument types. You might need to add explicit type casts."
    )
    assert e.position == at(sql, "=")


@pytest.mark.parametrize(
    "cond, message",
    [
        ("name < 1", "operator does not exist: text < integer"),
        ("name != 1", "operator does not exist: text <> integer"),
        ("5 <= name", "operator does not exist: integer <= text"),  # 원문 순서·연산자 그대로
        ("name = 3000000000", "operator does not exist: text = bigint"),
        ("name = 99999999999999999999", "operator does not exist: text = numeric"),
    ],
)
def test_text_operator_messages(catalog, cond, message):
    sql = f"SELECT * FROM users WHERE {cond}"
    e = err(sql, catalog, UndefinedFunction)
    assert e.message == message
    op = cond.split()[1]
    assert e.position == at(sql, op)


def test_where_text_and_null(catalog):
    b = B("SELECT * FROM users WHERE name = 'kim' AND name = NULL AND NULL = age", catalog)
    assert [c.value for c in b.where] == ["kim", None, None]


def test_select_limit(catalog):
    assert B("SELECT * FROM users LIMIT 5", catalog).limit == 5
    assert B("SELECT * FROM users LIMIT ALL", catalog).limit is None


def test_explain_binds_inner(catalog):
    b = B("EXPLAIN ANALYZE SELECT name FROM users", catalog)
    assert isinstance(b, BoundExplain) and b.analyze
    assert isinstance(b.statement, BoundSelect)
    with pytest.raises(UndefinedColumn):
        B("EXPLAIN SELECT nope FROM users", catalog)
