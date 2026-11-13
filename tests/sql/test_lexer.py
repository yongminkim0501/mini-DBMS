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
        ("x = $0", SqlSyntaxError, "42601", 'syntax error at or near "$0"', 5),
        ("x = $01", SqlSyntaxError, "42601", 'syntax error at or near "$01"', 5),
        ("x = $65536", SqlSyntaxError, "42601", 'syntax error at or near "$65536"', 5),
        ("x = $1abc", SqlSyntaxError, "42601", 'trailing junk after parameter at or near "$1abc"', 5),
        ("x = $", SqlSyntaxError, "42601", 'syntax error at or near "$"', 5),
        ("x = E'a\\n'", FeatureNotSupported, "0A000", "E'...' string literals are not supported", 5),
        ("x = $$a$$", FeatureNotSupported, "0A000", "dollar-quoted strings are not supported", 5),
    ],
)
def test_lex_errors(sql, exc, code, message, position):
    err = lex_error(sql, exc)
    assert err.sqlstate == code
    assert err.message == message
    assert err.position == position


def test_params():
    toks = tokenize("id = $1 AND x < $12")
    params = [(t.value, t.offset) for t in toks if t.type is T.PARAM]
    assert params == [(1, 5), (12, 16)]
    assert [t.type for t in tokenize("$65535")][0] is T.PARAM
    # $tag$ 는 여전히 dollar quote (미지원), 문자열 안의 $1 은 파라미터가 아니다
    assert [t.type for t in tokenize("'$1'")][:1] == [T.STRING]


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


# ------------------------------------------------------------------ 비 ASCII 비인용 식별자


def test_non_ascii_unquoted_identifiers():
    assert kinds("SELECT 이름 FROM 회원") == [
        (T.KEYWORD, "SELECT"), (T.IDENT, "이름"), (T.KEYWORD, "FROM"), (T.IDENT, "회원"),
    ]  # fmt: skip
    assert kinds("Users회원 회원1 _가$") == [(T.IDENT, "users회원"), (T.IDENT, "회원1"), (T.IDENT, "_가$")]


def test_non_ascii_uppercase_not_folded():
    assert kinds("Ä Σ ÄBC Straße") == [(T.IDENT, "Ä"), (T.IDENT, "Σ"), (T.IDENT, "Äbc"), (T.IDENT, "straße")]


def test_non_ascii_identifier_length_boundary():
    assert kinds("가" * 21) == [(T.IDENT, "가" * 21)]
    lex_error("가" * 22, NameTooLong)


def test_non_ascii_trailing_junk_after_number():
    err = lex_error("SELECT 123가", SqlSyntaxError)
    assert err.message == 'trailing junk after numeric literal at or near "123가"'


def test_non_ascii_identifier_positions():
    toks = tokenize("회원 이름")
    assert [(t.value, t.offset, t.column) for t in toks[:-1]] == [("회원", 0, 1), ("이름", 3, 4)]
