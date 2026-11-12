import pytest

from minidb.errors import FeatureNotSupported, InvalidRowCountInLimitClause, NameTooLong, SqlSyntaxError
from minidb.sql.ast import Condition, Default, InsertStmt, Literal, QualifiedName, SelectStmt, Star
from minidb.sql.lexer import split_statements
from minidb.sql.parser import parse_script, parse_statement

P = parse_statement
L = Literal
Q = QualifiedName


def cond(col, op, v):
    return Condition(col, op, L(v))


# ------------------------------------------------------------------ 정상 케이스 (왕복 테스트에도 사용)

VALID = {
    # INSERT
    "INSERT INTO users VALUES (1, 'a', 3)": InsertStmt(Q(None, "users"), None, ((L(1), L("a"), L(3)),), None),
    "insert into users (name, age) values ('홍길동', 30)": InsertStmt(
        Q(None, "users"), ("name", "age"), ((L("홍길동"), L(30)),), None
    ),
    "INSERT INTO users (name, age) VALUES ('a', 1), ('b', NULL), (NULL, -5)": InsertStmt(
        Q(None, "users"), ("name", "age"), ((L("a"), L(1)), (L("b"), L(None)), (L(None), L(-5))), None
    ),
    "INSERT INTO users VALUES (DEFAULT, 'x', DEFAULT)": InsertStmt(
        Q(None, "users"), None, ((Default(), L("x"), Default()),), None
    ),
    "INSERT INTO public.users (name) VALUES ('it''s') RETURNING *": InsertStmt(
        Q("public", "users"), ("name",), ((L("it's"),),), Star()
    ),
    "INSERT INTO users (name) VALUES ('a') RETURNING id, name": InsertStmt(
        Q(None, "users"), ("name",), ((L("a"),),), ("id", "name")
    ),
    'INSERT INTO "Users" ("select", "a""b") VALUES (\'\', \'😀\')': InsertStmt(
        Q(None, "Users"), ("select", 'a"b'), ((L(""), L("😀")),), None
    ),
    "INSERT INTO t VALUES (-9223372036854775808, 9223372036854775807)": InsertStmt(
        Q(None, "t"), None, ((L(-(2**63)), L(2**63 - 1)),), None
    ),
    "INSERT INTO t VALUES ('줄\n바꿈')": InsertStmt(Q(None, "t"), None, ((L("줄\n바꿈"),),), None),
    # SELECT
    "SELECT * FROM users": SelectStmt(Star(), Q(None, "users"), (), None),
    "select id, Name from Public.Users": SelectStmt(("id", "name"), Q("public", "users"), (), None),
    "SELECT * FROM users LIMIT 10": SelectStmt(Star(), Q(None, "users"), (), 10),
    "SELECT * FROM users LIMIT 0": SelectStmt(Star(), Q(None, "users"), (), 0),
    "SELECT * FROM users LIMIT ALL": SelectStmt(Star(), Q(None, "users"), (), None),
    "SELECT * FROM users WHERE id = 42": SelectStmt(Star(), Q(None, "users"), (cond("id", "=", 42),), None),
    "SELECT name FROM users WHERE a = 1 AND b <> 2 AND c != 3 AND d < 4 AND e <= 5 AND f > 6 AND g >= 7": SelectStmt(
        ("name",),
        Q(None, "users"),
        (
            cond("a", "=", 1), cond("b", "<>", 2), cond("c", "<>", 3), cond("d", "<", 4),
            cond("e", "<=", 5), cond("f", ">", 6), cond("g", ">=", 7),
        ),  # fmt: skip
        None,
    ),
    "SELECT * FROM t WHERE name = 'x' AND age = NULL AND n = -1 LIMIT 5": SelectStmt(
        Star(), Q(None, "t"), (cond("name", "=", "x"), cond("age", "=", None), cond("n", "=", -1)), 5
    ),
    # 리터럴이 왼쪽인 조건 → 컬럼이 왼쪽으로, 연산자 방향 반전
    "SELECT * FROM t WHERE 42 = id": SelectStmt(Star(), Q(None, "t"), (cond("id", "=", 42),), None),
    "SELECT * FROM t WHERE 5 < age": SelectStmt(Star(), Q(None, "t"), (cond("age", ">", 5),), None),
    "SELECT * FROM t WHERE 5 <= age AND 5 > a AND 5 >= b AND 5 <> c AND 5 != d": SelectStmt(
        Star(),
        Q(None, "t"),
        (cond("age", ">=", 5), cond("a", "<", 5), cond("b", "<=", 5), cond("c", "<>", 5), cond("d", "<>", 5)),
        None,
    ),
    "SELECT * FROM t WHERE 'kim' = name AND NULL = x AND -3 < y": SelectStmt(
        Star(), Q(None, "t"), (cond("name", "=", "kim"), cond("x", "=", None), cond("y", ">", -3)), None
    ),
    'SELECT "order", "Or" FROM "my table" WHERE "not" = 1': SelectStmt(
        ("order", "Or"), Q(None, "my table"), (cond("not", "=", 1),), None
    ),
    "SELECT a FROM t /* 주석 */ WHERE -- 줄 주석\n a = 1": SelectStmt(("a",), Q(None, "t"), (cond("a", "=", 1),), None),
}


@pytest.mark.parametrize("sql", list(VALID), ids=range(len(VALID)))
def test_valid(sql):
    assert P(sql) == VALID[sql]


@pytest.mark.parametrize("sql", list(VALID), ids=range(len(VALID)))
def test_roundtrip(sql):
    ast = P(sql)
    again = P(ast.to_sql())
    assert again == ast
    assert again.to_sql() == ast.to_sql()  # to_sql 은 정규형이라 고정점


def test_to_sql_examples():
    assert P("select * from T where 5 < AGE limit all").to_sql() == "SELECT * FROM t WHERE age > 5"
    assert P('insert into "Users" ("select") values (\'it\'\'s\')').to_sql() == (
        'INSERT INTO "Users" ("select") VALUES (\'it\'\'s\')'
    )
    assert P("SELECT a FROM t WHERE a != 1").to_sql() == "SELECT a FROM t WHERE a <> 1"


# ------------------------------------------------------------------ position


def test_positions():
    sql = "SELECT id, name FROM public.users WHERE 5 < age LIMIT 3"
    s = P(sql)
    assert s.position == 1
    assert s.column_positions == (8, 12)
    assert s.table.position == sql.index("public") + 1
    c = s.where[0]
    assert c.position == sql.index("age") + 1
    assert c.value.position == sql.index("5") + 1
    i = P("INSERT INTO t (a, b) VALUES (DEFAULT, -1) RETURNING b")
    assert i.column_positions == (16, 19)
    assert i.rows[0][0].position == 30 and i.rows[0][1].position == 39
    assert i.returning_positions == (53,)


def test_positions_in_script_are_absolute():
    sql = "SELECT 1 FROM a;\nSELECT x FROM t WHERE y = 'q'"
    stmts = parse_script(sql.replace("SELECT 1", "SELECT z"))
    assert stmts[1].column_positions == (sql.index("x FROM") + 1,)
    assert stmts[1].where[0].value.position == sql.index("'q'") + 1


# ------------------------------------------------------------------ 구문 오류


@pytest.mark.parametrize(
    "sql, message, position",
    [
        ("SELECT * FORM users", 'syntax error at or near "FORM"', 10),
        ("SELECT * FROM", "syntax error at end of input", 14),
        ("INSERT INTO t VALUES (1,", "syntax error at end of input", 25),
        ("INSERT INTO t VALUES (1, 2), (3)", "VALUES lists must all be the same length", 30),
        ("INSERT INTO t VALUES (1), (2, 3), (4)", "VALUES lists must all be the same length", 27),
        ("SELECT select FROM t", 'syntax error at or near "select"', 8),
        ("SELECT a FROM from", 'syntax error at or near "from"', 15),
        ("INSERT INTO t (values) VALUES (1)", 'syntax error at or near "values"', 16),
        ("SELECT a FROM t WHERE a = -'x'", "syntax error at or near \"'x'\"", 28),
        ("SELECT a FROM t WHERE a = -NULL", 'syntax error at or near "NULL"', 28),
        ("SELECT a FROM t WHERE a = 1 a", 'syntax error at or near "a"', 29),
        ("SELECT a, FROM t", 'syntax error at or near "FROM"', 11),
        ("SELECT 1", 'syntax error at or near "1"', 8),
        ("SELECT a FROM t WHERE", "syntax error at end of input", 22),
        ("SELECT a FROM t WHERE a 1", 'syntax error at or near "1"', 25),
        ("SELECT a FROM t LIMIT", "syntax error at end of input", 22),
        ("SELECT a FROM t LIMIT 'x'", "syntax error at or near \"'x'\"", 23),
        ("INSERT t VALUES (1)", 'syntax error at or near "t"', 8),
        ("INSERT INTO t VALUES ()", 'syntax error at or near ")"', 23),
        ("INSERT INTO t VALUES (1) RETURNING", "syntax error at end of input", 35),
        ("hello", 'syntax error at or near "hello"', 1),
        ("SELECT a FROM t;", 'syntax error at or near ";"', 16),
    ],
)
def test_syntax_errors(sql, message, position):
    with pytest.raises(SqlSyntaxError) as ei:
        P(sql)
    assert ei.value.sqlstate == "42601"
    assert ei.value.message == message
    assert ei.value.position == position


def test_lex_error_position_relative_to_statement_start():
    srcs = split_statements("SELECT a FROM t;\n  SELECT 'x' FROM t WHERE a = 1.5")
    with pytest.raises(FeatureNotSupported) as ei:
        P(srcs[1].text)  # 문자열로 넘기면 문장 기준
    assert ei.value.position == len("SELECT 'x' FROM t WHERE a = ") + 1


def test_name_too_long_through_parser():
    with pytest.raises(NameTooLong):
        P("SELECT * FROM " + "t" * 64)


# ------------------------------------------------------------------ 미지원 기능


@pytest.mark.parametrize(
    "sql, message",
    [
        ("UPDATE t SET a = 1", "UPDATE statements are not supported"),
        ("delete from t", "DELETE statements are not supported"),
        ("CREATE TABLE t (id bigint)", "CREATE statements are not supported"),
        ("DROP TABLE t", "DROP statements are not supported"),
        ("ALTER TABLE t ADD c text", "ALTER statements are not supported"),
        ("BEGIN", "BEGIN statements are not supported"),
        ("COMMIT", "COMMIT statements are not supported"),
        ("SELECT * FROM t WHERE a = 1 OR b = 2", "OR in WHERE clause is not supported"),
        ("SELECT * FROM t WHERE NOT a = 1", "NOT in WHERE clause is not supported"),
        ("SELECT * FROM t WHERE a IS NULL", "IS [NOT] NULL in WHERE clause is not supported"),
        ("SELECT * FROM t WHERE a IS NOT NULL", "IS [NOT] NULL in WHERE clause is not supported"),
        ("SELECT * FROM t WHERE (a = 1)", "parenthesized conditions in WHERE clause are not supported"),
        ("SELECT * FROM t WHERE a IN (1, 2)", "IN in WHERE clause is not supported"),
        ("SELECT * FROM t WHERE a LIKE 'x%'", "LIKE in WHERE clause is not supported"),
        ("SELECT * FROM t WHERE a = b", "comparison between two columns is not supported"),
        ("SELECT * FROM t ORDER BY a", "ORDER BY is not supported"),
        ("SELECT * FROM t WHERE a = 1 ORDER BY a", "ORDER BY is not supported"),
        ("SELECT a FROM t GROUP BY a", "GROUP BY is not supported"),
        ("SELECT * FROM a JOIN b ON a.id = b.id", "JOIN is not supported"),
        ("SELECT * FROM a LEFT JOIN b ON true", "JOIN is not supported"),
        ("SELECT * FROM a, b", "multiple tables in FROM clause are not supported"),
        ("SELECT * FROM t LIMIT 10 OFFSET 5", "OFFSET is not supported"),
        ("SELECT * FROM t OFFSET 5", "OFFSET is not supported"),
        ("SELECT count(*) FROM t", "function calls are not supported"),
        ("SELECT t.a FROM t", "qualified column names are not supported"),
        ("INSERT INTO t SELECT * FROM s", "INSERT ... SELECT is not supported"),
        ("INSERT INTO t DEFAULT VALUES", "DEFAULT VALUES is not supported"),
        ("SELECT * FROM db.public.t", "cross-database references are not supported"),
    ],
)
def test_feature_not_supported(sql, message):
    with pytest.raises(FeatureNotSupported) as ei:
        P(sql)
    assert ei.value.sqlstate == "0A000"
    assert ei.value.message == message
    assert ei.value.position is not None


def test_unsupported_position_points_at_feature():
    sql = "SELECT * FROM t WHERE a = 1 OR b = 2"
    with pytest.raises(FeatureNotSupported) as ei:
        P(sql)
    assert ei.value.position == sql.index("OR") + 1


@pytest.mark.parametrize("sql", ["SELECT * FROM t LIMIT -1", "SELECT * FROM t LIMIT -100"])
def test_negative_limit(sql):
    with pytest.raises(InvalidRowCountInLimitClause) as ei:
        P(sql)
    assert ei.value.sqlstate == "2201W"
    assert ei.value.message == "LIMIT must not be negative"
    assert ei.value.position == sql.index("-") + 1


# ------------------------------------------------------------------ parse_script


def test_parse_script():
    stmts = parse_script("INSERT INTO t VALUES (1); ; SELECT * FROM t -- x\n;")
    assert [type(s) for s in stmts] == [InsertStmt, SelectStmt]
    assert parse_script("  -- nothing\n ;") == []


def test_parse_script_stops_at_first_error():
    with pytest.raises(SqlSyntaxError) as ei:
        parse_script("SELECT * FROM a; SELECT * FORM b; SELECT 'unterminated")
    assert ei.value.position == len("SELECT * FROM a; SELECT * ") + 1


def test_parse_script_lex_error_after_valid_statements():
    sql = "SELECT * FROM a; SELECT 'oops"
    with pytest.raises(SqlSyntaxError) as ei:
        parse_script(sql)
    assert ei.value.message == "unterminated quoted string"
    assert ei.value.position == sql.index("'oops") + 1


def test_continue_on_error_pattern():
    # CLI 의 --on-error continue 가 쓸 조합: 문장별로 파싱하고 오류는 모아 둔다.
    results = []
    for src in split_statements("SELECT * FROM a; SELECT * FORM b; INSERT INTO c VALUES (1)"):
        try:
            results.append(type(parse_statement(src)).__name__)
        except SqlSyntaxError as e:
            results.append(f"error@{e.position}")
    assert results == ["SelectStmt", "error@27", "InsertStmt"]


def test_parser_instances_are_independent():
    # 같은 문장을 여러 번 파싱해도 결과가 같다 (공유 상태 없음)
    sql = "SELECT * FROM t WHERE 1 = a"
    assert P(sql) == P(sql) == P(split_statements(sql)[0])
