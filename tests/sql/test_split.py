from minidb.errors import SqlSyntaxError
from minidb.sql.lexer import split_statements


def texts(sql):
    return [(s.text, s.start_offset, s.error) for s in split_statements(sql)]


def test_basic_split_and_offsets():
    sql = "SELECT * FROM a;  INSERT INTO b VALUES (1);\nSELECT 2"
    got = split_statements(sql)
    assert [(s.text, s.start_offset) for s in got] == [
        ("SELECT * FROM a", 0),
        ("INSERT INTO b VALUES (1)", 18),
        ("SELECT 2", 44),
    ]
    for s in got:
        assert sql[s.start_offset : s.start_offset + len(s.text)] == s.text
        assert s.error is None


def test_semicolons_inside_strings_identifiers_comments():
    sql = "INSERT INTO t VALUES ('a;b'); SELECT \"x;y\" FROM t -- ; not here\n; /* ; */ SELECT 1"
    assert [t for t, _, _ in texts(sql)] == [
        "INSERT INTO t VALUES ('a;b')",
        'SELECT "x;y" FROM t',
        "SELECT 1",
    ]


def test_empty_statements_ignored():
    assert texts("") == []
    assert texts(";") == []
    assert texts(";;; ; \n ;") == []
    assert texts("-- only comment\n/* block */;") == []
    assert [t for t, _, _ in texts(";;SELECT 1;;;SELECT 2;;")] == ["SELECT 1", "SELECT 2"]


def test_trailing_semicolon_optional():
    assert [t for t, _, _ in texts("SELECT 1")] == ["SELECT 1"]
    assert [t for t, _, _ in texts("SELECT 1;")] == ["SELECT 1"]
    assert [t for t, _, _ in texts("SELECT 1 -- c")] == ["SELECT 1"]


def test_syntax_error_does_not_affect_other_statements():
    # 구문 오류는 분리 단계에서 검사하지 않는다
    got = texts("SELECT 1; SELECT * FORM t; SELECT 3")
    assert [t for t, _, _ in got] == ["SELECT 1", "SELECT * FORM t", "SELECT 3"]
    assert all(e is None for _, _, e in got)


def test_lex_error_keeps_previous_statements():
    sql = "SELECT 1; SELECT 2; SELECT 'oops; SELECT 4"
    got = split_statements(sql)
    assert [s.text for s in got[:2]] == ["SELECT 1", "SELECT 2"]
    last = got[-1]
    assert isinstance(last.error, SqlSyntaxError)
    assert last.error.message == "unterminated quoted string"
    assert last.error.position == sql.index("'oops") + 1  # 원문 기준
    assert last.start_offset == sql.index("SELECT 'oops")
    assert last.text == "SELECT 'oops; SELECT 4"
    assert len(got) == 3


def test_lex_error_at_statement_start():
    sql = "SELECT 1; /* never closed ; SELECT 2"
    got = split_statements(sql)
    assert [s.text for s in got[:-1]] == ["SELECT 1"]
    assert got[-1].error.message == "unterminated /* comment"
    assert got[-1].start_offset == sql.index("/*")
    assert got[-1].error.position == sql.index("/*") + 1


def test_tokens_are_kept_for_parser():
    (s,) = split_statements("  SELECT a FROM t ; ")
    assert s.tokens is not None
    assert [t.value for t in s.tokens] == ["SELECT", "a", "FROM", "t"]
    assert s.tokens[0].offset == 2  # 원문 기준


def test_iter_statements_matches_split():
    from minidb.sql.lexer import iter_statements

    sql = "SELECT 1; ;SELECT 'a;b'; SELECT 'oops"
    def key(srcs):
        return [(s.text, s.start_offset, s.error and s.error.message) for s in srcs]

    assert key(iter_statements(sql)) == key(split_statements(sql))


def test_base_offset_shifts_offsets_and_errors():
    from minidb.sql.lexer import iter_statements, iter_tokens

    toks = list(iter_tokens("SELECT a", base_offset=100))
    assert [(t.offset, t.end) for t in toks] == [(100, 106), (107, 108), (108, 108)]
    srcs = list(iter_statements("x; SELECT 'q", base_offset=1000))
    assert (srcs[0].text, srcs[0].start_offset) == ("x", 1000)
    assert srcs[0].tokens[0].offset == 1000
    assert (srcs[1].text, srcs[1].start_offset) == ("SELECT 'q", 1003)
    assert srcs[1].error.position == 1000 + len("x; SELECT ") + 1
