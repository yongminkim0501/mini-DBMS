import pytest

from minidb.errors import DatabaseError, SqlSyntaxError
from minidb.sql.errorfmt import display_width, format_error
from minidb.sql.parser import parse_script, parse_statement


def err_for(sql):
    with pytest.raises(DatabaseError) as ei:
        parse_script(sql)
    return ei.value


def caret_col(out):
    """출력 마지막 줄에서 캐럿의 표시 열(0부터)."""
    return out.splitlines()[-1].index("^")


def test_single_line():
    sql = "SELECT * FORM users;"
    assert format_error(sql, err_for(sql)) == (
        'ERROR:  syntax error at or near "FORM"\n'
        "LINE 1: SELECT * FORM users;\n"
        "                 ^"
    )


def test_end_of_input():
    sql = "SELECT * FROM"
    out = format_error(sql, err_for(sql))
    assert out.splitlines()[1] == "LINE 1: SELECT * FROM"
    assert caret_col(out) == len("LINE 1: SELECT * FROM")  # 마지막 글자 바로 뒤


def test_multiline_shows_only_error_line():
    sql = "SELECT *\nFROM users\nWHERE id = 1 OR id = 2;"
    out = format_error(sql, err_for(sql))
    assert out == (
        "ERROR:  OR in WHERE clause is not supported\n"
        "LINE 3: WHERE id = 1 OR id = 2;\n"
        "                     ^"
    )


def test_multiline_script_second_statement():
    sql = "SELECT * FROM a;\nSELECT * FORM b;"
    out = format_error(sql, err_for(sql))
    assert out.splitlines()[1] == "LINE 2: SELECT * FORM b;"
    assert caret_col(out) == len("LINE 2: SELECT * ")


def test_crlf_line_endings():
    sql = "SELECT *\r\nFORM t"
    out = format_error(sql, err_for(sql))
    assert out.splitlines()[1] == "LINE 2: FORM t"
    assert caret_col(out) == len("LINE 2: ")


def test_tabs_become_spaces():
    sql = "SELECT\t*\tFORM t"
    out = format_error(sql, err_for(sql))
    assert "\t" not in out
    assert out.splitlines()[1] == "LINE 1: SELECT * FORM t"
    assert caret_col(out) == len("LINE 1: SELECT * ")


def test_wide_characters_shift_caret():
    sql = "SELECT * FROM t WHERE name = '홍길동' OR a = 1"
    out = format_error(sql, err_for(sql))
    line = out.splitlines()[1]
    assert line == "LINE 1: " + sql
    # '홍길동' 3글자가 화면에서 6칸을 차지하므로 캐럿은 문자 수보다 3칸 오른쪽
    assert caret_col(out) == len("LINE 1: ") + sql.index("OR") + 3
    assert display_width("홍길동") == 6
    assert display_width("😀") == 2
    assert display_width("é") == 1  # 결합 문자는 폭 0


def test_no_position():
    assert format_error("x", DatabaseError("boom")) == "ERROR:  boom"


def test_long_line_truncated_after_caret():
    sql = "SELECT * FORM t WHERE " + " AND ".join(f"c{i} = {i}" for i in range(40))
    out = format_error(sql, err_for(sql))
    line = out.splitlines()[1]
    assert line.startswith("LINE 1: SELECT * FORM t") and line.endswith("...")
    assert display_width(line) <= len("LINE 1: ") + 60 + 3
    assert caret_col(out) == len("LINE 1: SELECT * ")


def test_long_line_truncated_both_sides():
    sql = "SELECT * FROM t WHERE " + " AND ".join(f"c{i} = {i}" for i in range(30)) + " OR x = 1 AND " + " AND ".join(
        f"d{i} = {i}" for i in range(30)
    )
    out = format_error(sql, err_for(sql))
    line = out.splitlines()[1]
    assert line.startswith("LINE 1: ...") and line.endswith("...")
    shown = line[len("LINE 1: ...") : -3]
    assert len(shown) <= 60
    col = caret_col(out)
    assert line[col : col + 2] == "OR"  # 잘라낸 뒤에도 캐럿이 정확히 OR 을 가리킨다
    assert len(shown) - (col - len("LINE 1: ...")) >= 10  # 캐럿 오른쪽 최소 10칸


def test_long_line_with_wide_chars_caret_alignment():
    sql = "SELECT * FROM t WHERE name = '" + "가" * 40 + "' OR a = 1"
    out = format_error(sql, err_for(sql))
    line = out.splitlines()[1]
    col = caret_col(out)
    # 표시 폭 기준으로 캐럿 앞 부분을 재면 OR 직전이어야 한다
    acc = 0
    for i, ch in enumerate(line):
        if acc == col:
            assert line[i : i + 2] == "OR"
            break
        acc += display_width(ch)
    else:
        pytest.fail("caret column not aligned to a character")


def test_position_on_lex_error():
    sql = "SELECT a FROM t;\nSELECT 'never closed"
    out = format_error(sql, err_for(sql))
    assert out.splitlines()[0] == "ERROR:  unterminated quoted string"
    assert out.splitlines()[1] == "LINE 2: SELECT 'never closed"
    assert caret_col(out) == len("LINE 2: SELECT ")


def test_statement_text_position():
    # parse_statement 에 문자열로 넘긴 경우에도 동작
    sql = "INSERT INTO t VALUES (1, 2), (3)"
    with pytest.raises(SqlSyntaxError) as ei:
        parse_statement(sql)
    assert caret_col(format_error(sql, ei.value)) == len("LINE 1: INSERT INTO t VALUES (1, 2), ")
