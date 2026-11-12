import io

import pytest

from minidb.sql.errorfmt import format_error
from minidb.sql.lexer import split_statements
from minidb.sql.parser import parse_statement
from minidb.sql.stream import iter_script_statements

SCRIPT = (
    "-- header comment\n"
    "INSERT INTO t VALUES ('a;b'), ('c');\n"
    "SELECT * FROM t WHERE x = 'multi\nline';  SELECT 2 FROM t;\n"
    "/* ; */ ;;\n"
    "SELECT * FROM t -- tail\n"
)


def stream(text, chunk):
    return list(iter_script_statements(io.StringIO(text), chunk_size=chunk))


@pytest.mark.parametrize("chunk", [1, 2, 3, 7, 16, 1000])
def test_matches_split_statements_for_any_chunk_size(chunk):
    expected = [(s.text, s.start_offset) for s in split_statements(SCRIPT)]
    got = stream(SCRIPT, chunk)
    assert [(s.source.text, s.source.start_offset) for s in got] == expected
    for s in got:
        assert s.source.error is None
        assert SCRIPT[s.source.start_offset : s.source.start_offset + len(s.source.text)] == s.source.text
        # 토큰 offset 도 파일 전체 기준
        assert s.source.tokens[0].offset == s.source.start_offset


@pytest.mark.parametrize("chunk", [1, 5, 1000])
def test_line_numbers_and_prefix(chunk):
    got = stream(SCRIPT, chunk)
    assert [s.line for s in got] == [2, 3, 4, 6]
    assert got[2].line_prefix == "line';  "
    assert got[0].line_prefix == ""


@pytest.mark.parametrize("chunk", [1, 4, 1000])
def test_error_position_formats_with_file_line(chunk):
    text = "SELECT * FROM a;\n\n\nSELECT x,\n  y FORM t;\nSELECT 1 FROM b"
    st = stream(text, chunk)[1]
    with pytest.raises(Exception) as ei:
        parse_statement(st.source)
    out = format_error(st.context, ei.value, position_offset=st.context_offset, line_offset=st.line - 1)
    assert out.splitlines()[1] == "LINE 5:   y FORM t;"
    assert out.splitlines()[2].index("^") == len("LINE 5:   y ")


@pytest.mark.parametrize("chunk", [1, 3, 1000])
def test_lex_error_at_eof_after_good_statements(chunk):
    text = "SELECT a FROM t; SELECT 'never closed; SELECT b FROM t"
    got = stream(text, chunk)
    assert [s.source.text for s in got[:-1]] == ["SELECT a FROM t"]
    last = got[-1]
    assert last.source.error.message == "unterminated quoted string"
    assert last.source.error.position == text.index("'never") + 1
    assert last.source.start_offset == text.index("SELECT 'never")


def test_hard_lex_error_reported_without_reading_to_eof():
    # 청크 경계와 무관한 오류(알 수 없는 문자)는 파일 끝까지 버퍼링하지 않고 바로 확정한다
    big_tail = "".join(f"SELECT c{i} FROM t;\n" for i in range(2000))
    text = "SELECT a FROM t;\nSELECT # FROM t;\n" + big_tail
    reader = io.StringIO(text)
    it = iter_script_statements(reader, chunk_size=200)
    first = next(it)
    second = next(it)
    assert first.source.text == "SELECT a FROM t"
    assert second.source.error is not None and second.line == 2
    assert reader.tell() < len(text) // 2  # 뒷부분은 아직 읽지 않았다


def test_statement_larger_than_chunk():
    rows = ", ".join(f"({i}, 'name{i}')" for i in range(3000))
    text = f"INSERT INTO t VALUES {rows};\nSELECT * FROM t;"
    got = stream(text, 100)
    assert len(got) == 2
    assert len(parse_statement(got[0].source).rows) == 3000
    assert got[1].line == 2


def test_empty_and_comment_only():
    assert stream("", 10) == []
    assert stream("  -- x\n /* y */ ;; ", 3) == []


def test_crlf_and_korean():
    text = "SELECT 이름 FROM 회원;\r\nSELECT * FORM t;"
    got = stream(text, 4)
    assert got[1].line == 2 and got[1].source.text == "SELECT * FORM t"
