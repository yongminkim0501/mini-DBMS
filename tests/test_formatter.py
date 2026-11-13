from minidb.formatter import format_cell, format_table, row_count_footer
from minidb.result import ColumnInfo
from minidb.sql.errorfmt import display_width

ID = ColumnInfo("id", "bigint", 20)
NAME = ColumnInfo("name", "text", 25)
AGE = ColumnInfo("age", "bigint", 20)


def test_basic_alignment():
    assert format_table([ID, NAME, AGE], [(1, "kim", 25), (10, "lee", 3)]) == [
        " id | name | age ",
        "----+------+-----",
        "  1 | kim  |  25",
        " 10 | lee  |   3",
        "(2 rows)",
    ]


def test_numbers_right_text_left_last_text_not_padded():
    assert format_table([NAME, ID], [("a", 1), ("abcdef", 12345)]) == [
        "  name  |  id   ",
        "--------+-------",
        " a      |     1",
        " abcdef | 12345",
        "(2 rows)",
    ]
    lines = format_table([ID, NAME], [(1, "a"), (2, "abcdef")])
    assert lines[2] == "  1 | a"  # 마지막 text 칸은 뒤를 채우지 않음
    assert lines[3] == "  2 | abcdef"


def test_header_centering_odd_padding_goes_right():
    lines = format_table([ColumnInfo("ab", "text", 25)], [("abcde",)])
    assert lines[0] == "  ab   "  # 폭 5, 남는 3칸 → 왼쪽 1, 오른쪽 2 (+ 테두리 공백)


def test_null_is_blank():
    assert format_table([ID, NAME, AGE], [(1, None, None), (2, "x", 5)]) == [
        " id | name | age ",
        "----+------+-----",
        "  1 |      |    ",
        "  2 | x    |   5",
        "(2 rows)",
    ]
    assert format_table([ID, NAME], [(1, None)])[2] == "  1 | "


def test_korean_width():
    lines = format_table([ID, NAME, AGE], [(1, "홍길동", 30), (2, "kim", 5)])
    assert lines == [
        " id |  name  | age ",
        "----+--------+-----",
        "  1 | 홍길동 |  30",
        "  2 | kim    |   5",
        "(2 rows)",
    ]
    # 모든 줄(헤더/구분선)의 "|" 위치가 화면에서 맞는다
    assert display_width(lines[2].split("|")[1]) == display_width(lines[3].split("|")[1]) == 8


def test_korean_header():
    lines = format_table([ColumnInfo("이름", "text", 25)], [("a",)])
    assert lines[:2] == [" 이름 ", "------"]


def test_row_counts():
    assert format_table([ID], [])[-1] == "(0 rows)"
    assert format_table([ID], [])[:2] == [" id ", "----"]
    assert format_table([ID], [(1,)])[-1] == "(1 row)"
    assert format_table([ID], [(i,) for i in range(5)])[-1] == "(5 rows)"
    assert row_count_footer(1) == "(1 row)" and row_count_footer(2) == "(2 rows)"


def test_multiline_values():
    assert format_table([NAME, ID], [("a\nbb", 1)]) == [
        " name | id ",
        "------+----",
        " a   +|  1",
        " bb   | ",  # 끝난 마지막 칸은 채우지 않는다 (실제 psql 17 출력으로 확인)
        "(1 row)",
    ]
    assert format_table([ID, NAME], [(1, "x\ny")]) == [
        " id | name ",
        "----+------",
        "  1 | x   +",
        "    | y",
        "(1 row)",
    ]


def test_explain_like_single_text_column():
    col = ColumnInfo("QUERY PLAN", "text", 25)
    assert format_table([col], [("Limit",), ("  ->  Seq Scan on users",)]) == [
        "       QUERY PLAN        ",
        "-------------------------",
        " Limit",
        "   ->  Seq Scan on users",
        "(2 rows)",
    ]


def test_format_cell():
    assert format_cell(None) == "" and format_cell(-5) == "-5" and format_cell("x") == "x"


def test_multiline_cell_with_exhausted_last_numeric_column():
    # psql 과 같은 출력 (실제 psql 로 확인): 이어지는 줄에서 끝난 마지막 숫자 칸은 채우지 않는다
    lines = format_table([NAME, AGE], [("a\nbb", 2), ("c", None)])
    assert lines == [" name | age ", "------+-----", " a   +|   2", " bb   | ", " c    |    ", "(2 rows)"]
