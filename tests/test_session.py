"""세션 설정과 SET / SHOW (문법, 오류, 실행)."""

import json

import pytest

from minidb.engine import Database
from minidb.errors import (
    FeatureNotSupported,
    InvalidParameterValue,
    SqlSyntaxError,
    UndefinedObject,
)
from minidb.session import SETTINGS, Session, parse_bool
from minidb.sql.ast import Default, Literal, SetStmt, ShowStmt
from minidb.sql.parser import parse_statement

# ------------------------------------------------------------------ parse_bool (PostgreSQL 규칙)


@pytest.mark.parametrize(
    "text",
    ["on", "ON", "On", "true", "TRUE", "t", "tr", "tru", "yes", "y", "ye", "1"],
)
def test_parse_bool_true(text):
    assert parse_bool(text) is True


@pytest.mark.parametrize("text", ["off", "OFF", "of", "false", "f", "fa", "fals", "no", "n", "0"])
def test_parse_bool_false(text):
    assert parse_bool(text) is False


@pytest.mark.parametrize("text", ["", "o", "2", "-1", "10", "01", "onn", "offf", "truee", "yess", " on", "on ", "maybe"])
def test_parse_bool_invalid(text):
    assert parse_bool(text) is None


# ------------------------------------------------------------------ Session


def test_session_defaults_and_overrides():
    s = Session()
    assert s.enable_indexscan is True
    assert s.get("enable_indexscan") is True
    assert s.show("ENABLE_INDEXSCAN") == ("enable_indexscan", "on")
    s2 = Session(enable_indexscan=False)
    assert s2.enable_indexscan is False and s.enable_indexscan is True  # 세션끼리 독립
    with pytest.raises(UndefinedObject):
        Session(nope=1)
    assert Session(enable_indexscan="off").enable_indexscan is False
    assert Session(enable_indexscan=0).enable_indexscan is False
    with pytest.raises(InvalidParameterValue):
        Session(enable_indexscan="maybe")


def test_session_set_values():
    s = Session()
    s.set("enable_indexscan", "off")
    assert s.enable_indexscan is False
    s.set("Enable_IndexScan", 1)
    assert s.enable_indexscan is True
    s.set("enable_indexscan", 0)
    assert s.enable_indexscan is False
    s.set("enable_indexscan", None)  # DEFAULT
    assert s.enable_indexscan is True


def test_session_set_errors():
    s = Session()
    with pytest.raises(UndefinedObject) as ei:
        s.set("enable_seqscan", "on", name_position=5)
    assert ei.value.sqlstate == "42704"
    assert ei.value.message == 'unrecognized configuration parameter "enable_seqscan"'
    assert ei.value.position == 5
    with pytest.raises(InvalidParameterValue) as ei:
        s.set("ENABLE_INDEXSCAN", "maybe", value_position=9)
    assert ei.value.sqlstate == "22023"
    # 메시지는 정식 이름 (PostgreSQL 과 같다)
    assert ei.value.message == 'parameter "enable_indexscan" requires a Boolean value'
    assert ei.value.position == 9
    with pytest.raises(InvalidParameterValue):
        s.set("enable_indexscan", 2)
    assert s.enable_indexscan is True  # 실패한 SET 은 값을 바꾸지 않는다


def test_settings_registry_is_read_only():
    with pytest.raises(TypeError):
        SETTINGS["x"] = None  # type: ignore[index]


# ------------------------------------------------------------------ 문법


@pytest.mark.parametrize(
    ("sql", "name", "value"),
    [
        ("SET enable_indexscan = off", "enable_indexscan", Literal("off")),
        ("SET enable_indexscan TO on", "enable_indexscan", Literal("on")),
        ("set Enable_IndexScan to OFF", "enable_indexscan", Literal("off")),
        ("SET enable_indexscan = 'off'", "enable_indexscan", Literal("off")),
        ("SET enable_indexscan = true", "enable_indexscan", Literal("true")),
        ("SET enable_indexscan = 0", "enable_indexscan", Literal(0)),
        ("SET enable_indexscan = -1", "enable_indexscan", Literal(-1)),
        ("SET enable_indexscan TO DEFAULT", "enable_indexscan", Default()),
        ("SET enable_indexscan = DEFAULT", "enable_indexscan", Default()),
        ('SET "enable_indexscan" = "off"', "enable_indexscan", Literal("off")),
        ("SET SESSION enable_indexscan = off", "enable_indexscan", Literal("off")),
        ("SET my.custom = 'x'", "my.custom", Literal("x")),
        ("SET session = on", "session", Literal("on")),  # 다음이 '=' 면 SESSION 은 이름
    ],
)
def test_parse_set(sql, name, value):
    stmt = parse_statement(sql)
    assert stmt == SetStmt(name, value)
    assert parse_statement(stmt.to_sql()) == stmt


def test_parse_set_positions():
    stmt = parse_statement("SET  enable_indexscan =  off")
    assert stmt.position == 1 and stmt.name_position == 6 and stmt.value.position == 26


@pytest.mark.parametrize(
    ("sql", "name"),
    [("SHOW enable_indexscan", "enable_indexscan"), ("show ENABLE_INDEXSCAN", "enable_indexscan"), ('SHOW "X"', "X")],
)
def test_parse_show(sql, name):
    stmt = parse_statement(sql)
    assert stmt == ShowStmt(name)
    assert parse_statement(stmt.to_sql()) == stmt


@pytest.mark.parametrize(
    ("sql", "exc", "message", "position"),
    [
        ("SET", SqlSyntaxError, "syntax error at end of input", 4),
        ("SET enable_indexscan", SqlSyntaxError, "syntax error at end of input", 21),
        ("SET enable_indexscan off", SqlSyntaxError, 'syntax error at or near "off"', 22),
        ("SET enable_indexscan =", SqlSyntaxError, "syntax error at end of input", 23),
        ("SET enable_indexscan = NULL", SqlSyntaxError, 'syntax error at or near "NULL"', 24),
        ("SET enable_indexscan = on off", SqlSyntaxError, 'syntax error at or near "off"', 27),
        ("SET enable_indexscan = on, off", InvalidParameterValue, "SET enable_indexscan takes only one argument", 26),
        ("SET LOCAL enable_indexscan = on", FeatureNotSupported, "SET LOCAL is not supported", 5),
        ("SHOW", SqlSyntaxError, "syntax error at end of input", 5),
        ("SHOW ALL", FeatureNotSupported, "SHOW ALL is not supported", 6),
        ("SHOW a b", SqlSyntaxError, 'syntax error at or near "b"', 8),
        ("EXPLAIN SET enable_indexscan = on", SqlSyntaxError, 'syntax error at or near "SET"', 9),
        ("RESET enable_indexscan", FeatureNotSupported, "RESET statements are not supported", 1),
    ],
)
def test_parse_set_show_errors(sql, exc, message, position):
    with pytest.raises(exc) as ei:
        parse_statement(sql)
    assert ei.value.message == message
    assert ei.value.position == position


def test_to_is_reserved():
    # TO 는 PostgreSQL 에서도 예약어: 컬럼 이름으로 쓰려면 인용해야 한다
    with pytest.raises(SqlSyntaxError, match='"to"'):
        parse_statement("SELECT to FROM t")
    assert parse_statement('SELECT "to" FROM t').columns == ("to",)


def test_set_show_are_not_reserved():
    stmt = parse_statement("SELECT set, show FROM t")
    assert stmt.columns == ("set", "show")


# ------------------------------------------------------------------ 실행


@pytest.fixture
def db(tmp_path):
    (tmp_path / "schema").mkdir()
    doc = {
        "oid": 1,
        "table": "users",
        "columns": [
            {"name": "id", "type": "BIGINT", "auto_increment": True},
            {"name": "name", "type": "TEXT"},
        ],
    }
    (tmp_path / "schema" / "1.json").write_text(json.dumps(doc))
    with Database(tmp_path) as d:
        yield d


def test_execute_set_show(db):
    r = db.execute_one("SHOW enable_indexscan")
    assert [c.name for c in r.columns] == ["enable_indexscan"]
    assert r.columns[0].type_name == "text"
    assert r.fetchall() == [("on",)]
    assert r.command_tag == "SHOW"
    r = db.execute_one("SET enable_indexscan = off")
    assert r.columns is None and r.command_tag == "SET"
    assert db.session.enable_indexscan is False
    assert db.execute_one("SHOW ENABLE_INDEXSCAN").fetchall() == [("off",)]
    db.execute_one("SET enable_indexscan TO DEFAULT")
    assert db.session.enable_indexscan is True


def test_execute_with_explicit_sessions(db):
    a, b = Session(), Session()
    list(db.execute("SET enable_indexscan = off", a))
    assert a.enable_indexscan is False
    assert b.enable_indexscan is True and db.session.enable_indexscan is True
    plan_a = [r[0] for r in db.execute_one("EXPLAIN SELECT * FROM users WHERE id = 1", a).fetchall()]
    plan_b = [r[0] for r in db.execute_one("EXPLAIN SELECT * FROM users WHERE id = 1", b).fetchall()]
    assert plan_a[0] == "Seq Scan on users"
    assert plan_b[0] == "Index Scan using users_pkey on users"


def test_execute_set_errors_with_positions(db):
    sql = "SELECT * FROM users;\nSET nope = on;\nSET enable_indexscan = maybe"
    results = list(db.execute(sql))
    err1, err2 = results[1], results[2]
    assert isinstance(err1, UndefinedObject)
    assert err1.position == sql.index("nope") + 1
    assert isinstance(err2, InvalidParameterValue)
    assert err2.position == sql.index("maybe") + 1
    assert db.session.enable_indexscan is True
