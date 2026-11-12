import pytest

from minidb.errors import FeatureNotSupported, NameTooLong, SqlSyntaxError
from minidb.sql.lexer import tokenize
from minidb.sql.tokens import TokenType as T


def kinds(sql):
    return [(t.type, t.value) for t in tokenize(sql)[:-1]]


def lex_error(sql, exc):
    with pytest.raises(exc) as ei:
        tokenize(sql)
    return ei.value


# ------------------------------------------------------------------ 키워드/식별자


def test_keywords_case_insensitive():
    assert kinds("select SeLeCt SELECT") == [(T.KEYWORD, "SELECT")] * 3
    assert kinds("from where insert into values returning limit all and null default") == [
        (T.KEYWORD, k)
        for k in ["FROM", "WHERE", "INSERT", "INTO", "VALUES", "RETURNING", "LIMIT", "ALL", "AND", "NULL", "DEFAULT"]
    ]


def test_unquoted_identifiers_lowercased():
    assert kinds("Users _x a1$b") == [(T.IDENT, "users"), (T.IDENT, "_x"), (T.IDENT, "a1$b")]
    toks = tokenize("Users")
    assert toks[0].quoted is False


def test_quoted_identifier_keeps_case_and_escapes():
    toks = tokenize('"MyTable" "a""b" "select" "공백 있는 이름"')
    assert [(t.type, t.value, t.quoted) for t in toks[:-1]] == [
        (T.IDENT, "MyTable", True),
        (T.IDENT, 'a"b', True),
        (T.IDENT, "select", True),  # 인용하면 키워드가 아니라 식별자
        (T.IDENT, "공백 있는 이름", True),
    ]


def test_symbols():
    assert [t.type for t in tokenize("( ) , ; * . = <> != < <= > >= -")[:-1]] == [
        T.LPAREN, T.RPAREN, T.COMMA, T.SEMICOLON, T.STAR, T.DOT, T.EQ,
        T.NE, T.NE, T.LT, T.LE, T.GT, T.GE, T.MINUS,
    ]  # fmt: skip
    assert kinds("a<=b") == [(T.IDENT, "a"), (T.LE, "<="), (T.IDENT, "b")]


def test_integers():
    assert kinds("0 42 9223372036854775808 99999999999999999999999") == [
        (T.INTEGER, 0), (T.INTEGER, 42), (T.INTEGER, 2**63), (T.INTEGER, 99999999999999999999999),
    ]  # fmt: skip
    assert kinds("-5") == [(T.MINUS, "-"), (T.INTEGER, 5)]  # 음수는 파서가 합친다


# ------------------------------------------------------------------ 문자열


def test_string_escapes_and_newlines():
    assert kinds("'it''s' '' ''''") == [(T.STRING, "it's"), (T.STRING, ""), (T.STRING, "'")]
    assert kinds("'line1\nline2'") == [(T.STRING, "line1\nline2")]
    assert kinds("'홍길동 😀👍🏽'") == [(T.STRING, "홍길동 😀👍🏽")]
    assert kinds("'a;b' 'c--d' 'e/*f'") == [(T.STRING, "a;b"), (T.STRING, "c--d"), (T.STRING, "e/*f")]


# ------------------------------------------------------------------ 주석


def test_comments():
    assert kinds("a -- comment ; 'x\nb") == [(T.IDENT, "a"), (T.IDENT, "b")]
    assert kinds("a -- 끝까지") == [(T.IDENT, "a")]
    assert kinds("a /* x */ b") == [(T.IDENT, "a"), (T.IDENT, "b")]
    assert kinds("a /* x /* nested ; ' \" */ still comment */ b") == [(T.IDENT, "a"), (T.IDENT, "b")]
    assert kinds("a/**/b") == [(T.IDENT, "a"), (T.IDENT, "b")]
    assert kinds("a - -b") == [(T.IDENT, "a"), (T.MINUS, "-"), (T.MINUS, "-"), (T.IDENT, "b")]


# ------------------------------------------------------------------ 위치


def test_offsets_lines_columns():
    sql = "SELECT a,\n  \"B\"\nFROM t -- c\n/* x\ny */ WHERE 'p\nq' = 1"
    toks = tokenize(sql)
    got = [(t.value, t.offset, t.line, t.column) for t in toks]
    assert got == [
        ("SELECT", 0, 1, 1),
        ("a", 7, 1, 8),
        (",", 8, 1, 9),
        ("B", 12, 2, 3),
        ("FROM", 16, 3, 1),
        ("t", 21, 3, 6),
        ("WHERE", 38, 5, 6),
        ("p\nq", 44, 5, 12),
        ("=", 50, 6, 4),
        (1, 52, 6, 6),
        (None, 53, 6, 7),
    ]
    for t in toks[:-1]:
        assert sql[t.offset : t.end] in {"SELECT", "a", ",", '"B"', "FROM", "t", "WHERE", "'p\nq'", "=", "1"}


def test_column_counts_characters_not_bytes():
    toks = tokenize("'한글' x")
    assert toks[1].column == 6 and toks[1].offset == 5


# ------------------------------------------------------------------ 오류


@pytest.mark.parametrize(
    "sql, exc, code, message, position",
    [
        ("SELECT 'abc", SqlSyntaxError, "42601", "unterminated quoted string", 8),
        ("SELECT 'abc''", SqlSyntaxError, "42601", "unterminated quoted string", 8),
        ('SELECT "abc', SqlSyntaxError, "42601", "unterminated quoted identifier", 8),
        ('SELECT ""', SqlSyntaxError, "42601", "zero-length delimited identifier", 8),
        ("a /* x /* y */", SqlSyntaxError, "42601", "unterminated /* comment", 3),
        ("SELECT # FROM", SqlSyntaxError, "42601", 'syntax error at or near "#"', 8),
        ("x ! y", SqlSyntaxError, "42601", 'syntax error at or near "!"', 3),
        ("SELECT 123abc", SqlSyntaxError, "42601", 'trailing junk after numeric literal at or near "123abc"', 8),
        ("x = 1.5", FeatureNotSupported, "0A000", "numeric literals are not supported", 5),
        ("x = 1e3", FeatureNotSupported, "0A000", "numeric literals are not supported", 5),
        ("x = .5", FeatureNotSupported, "0A000", "numeric literals are not supported", 5),
        ("x = $1", FeatureNotSupported, "0A000", "parameters ($1) are not supported", 5),
        ("x = E'a\\n'", FeatureNotSupported, "0A000", "E'...' string literals are not supported", 5),
        ("x = $$a$$", FeatureNotSupported, "0A000", "dollar-quoted strings are not supported", 5),
    ],
)
def test_lex_errors(sql, exc, code, message, position):
    err = lex_error(sql, exc)
    assert err.sqlstate == code
    assert err.message == message
    assert err.position == position


def test_error_position_is_character_based():
    err = lex_error("'한글' #", SqlSyntaxError)
    assert err.position == 6


# ------------------------------------------------------------------ 63바이트 경계


def test_identifier_length_boundary():
    assert kinds("a" * 63) == [(T.IDENT, "a" * 63)]
    err = lex_error("x " + "a" * 64, NameTooLong)
    assert err.sqlstate == "42622" and err.position == 3
    assert kinds('"' + "가" * 21 + '"') == [(T.IDENT, "가" * 21)]  # 63바이트
    lex_error('"' + "가" * 22 + '"', NameTooLong)  # 66바이트
    lex_error('"' + "A" * 64 + '"', NameTooLong)
    assert kinds('"' + "A" * 63 + '"') == [(T.IDENT, "A" * 63)]
