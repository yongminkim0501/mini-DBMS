import io
import re
import shutil
from pathlib import Path

import pytest

from minidb.__main__ import format_timing, main

SCHEMA = Path(__file__).resolve().parents[1] / "db" / "schema" / "16384.json"


@pytest.fixture
def db(tmp_path):
    d = tmp_path / "db"
    (d / "schema").mkdir(parents=True)
    shutil.copy(SCHEMA, d / "schema" / "16384.json")
    return d


def run(args, stdin_text=None):
    out, err = io.StringIO(), io.StringIO()
    stdin = io.StringIO(stdin_text) if stdin_text is not None else None
    code = main(args, stdin=stdin, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def write(tmp_path, text, name="s.sql"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


# ------------------------------------------------------------------ 종료 코드


def test_exit_0_on_success(db, tmp_path):
    code, out, err = run(["run", write(tmp_path, "INSERT INTO users (name) VALUES ('a'); SELECT name FROM users;"), "--db", str(db)])
    assert (code, err) == (0, "")
    assert out == "INSERT 0 1\n name \n------\n a\n(1 row)\n\n"


def test_exit_1_on_statement_error(db, tmp_path):
    code, out, err = run(["run", write(tmp_path, "SELECT * FROM nope;"), "--db", str(db)])
    assert code == 1
    assert err.startswith('ERROR:  relation "nope" does not exist\nLINE 1: SELECT * FROM nope;\n')


@pytest.mark.parametrize(
    "args",
    [
        [],  # -c 도 run 도 없음
        ["run"],  # 파일 없음
        ["run", "x.sql"],  # --db 없음
        ["-c", "SELECT 1"],  # --db 없음
        ["run", "x.sql", "--db", "d", "--on-error", "maybe"],
        ["bogus"],
    ],
)
def test_exit_2_on_usage_error(args, capsys):
    assert run(args)[0] == 2


def test_exit_2_on_missing_file_or_db(db, tmp_path):
    code, _, err = run(["run", str(tmp_path / "missing.sql"), "--db", str(db)])
    assert code == 2 and "could not open file" in err
    code, _, err = run(["-c", "SELECT * FROM users", "--db", str(tmp_path / "no-such-db")])
    assert code == 2 and "does not exist" in err


def test_exit_2_on_invalid_utf8(db, tmp_path):
    p = tmp_path / "bad.sql"
    p.write_bytes(b"SELECT * FROM users WHERE name = '\xff';")
    code, _, err = run(["run", str(p), "--db", str(db)])
    assert code == 2 and "UTF-8" in err


def test_exit_2_on_broken_catalog(db):
    (db / "schema" / "x.json").write_text("{bad")
    code, _, err = run(["-c", "SELECT * FROM users", "--db", str(db)])
    assert code == 2 and "x.json" in err


# ------------------------------------------------------------------ --on-error


SCRIPT = "INSERT INTO users (name) VALUES ('a');\nSELECT * FORM users;\nINSERT INTO users (name) VALUES ('b');\n"


def test_on_error_stop_is_default(db, tmp_path):
    code, out, err = run(["run", write(tmp_path, SCRIPT), "--db", str(db)])
    assert code == 1
    assert out == "INSERT 0 1\n"  # 세 번째 문장은 실행되지 않음
    assert "LINE 2: SELECT * FORM users;" in err
    _, out, _ = run(["-c", "SELECT name FROM users", "--db", str(db)])
    assert "(1 row)" in out


def test_on_error_continue(db, tmp_path):
    code, out, err = run(["run", write(tmp_path, SCRIPT), "--db", str(db), "--on-error", "continue"])
    assert code == 1
    assert out == "INSERT 0 1\nINSERT 0 1\n"
    assert err.count("ERROR:") == 1
    _, out, _ = run(["-c", "SELECT name FROM users", "--db", str(db)])
    assert "(2 rows)" in out


# ------------------------------------------------------------------ 입력 방식


def test_stdin(db):
    code, out, _ = run(["run", "-", "--db", str(db)], stdin_text="INSERT INTO users (name) VALUES ('from stdin') RETURNING name;")
    assert code == 0
    assert out == "    name    \n------------\n from stdin\n(1 row)\n\nINSERT 0 1\n"


def test_dash_c_multiple_statements(db):
    code, out, err = run(["-c", "INSERT INTO users (name, age) VALUES ('x', 1); SELECT age FROM users", "--db", str(db)])
    assert code == 0 and err == ""
    assert out == "INSERT 0 1\n age \n-----\n   1\n(1 row)\n\n"


def test_dash_c_error_line_numbers(db):
    code, _, err = run(["-c", "SELECT * FROM users;\nSELECT * FROM users WHERE name = 1", "--db", str(db)])
    assert code == 1
    assert "LINE 2: SELECT * FROM users WHERE name = 1" in err
    assert "HINT:  No operator matches" in err


def test_error_line_numbers_are_file_wide(db, tmp_path):
    text = "".join(f"INSERT INTO users (name) VALUES ('{i}');\n" for i in range(500)) + "SELECT * FROM users WHERE age = 'x';\n"
    code, _, err = run(["run", write(tmp_path, text), "--db", str(db)])
    assert code == 1
    assert "LINE 501: SELECT * FROM users WHERE age = 'x';" in err


# ------------------------------------------------------------------ --echo / --timing / --fsync


def test_echo(db):
    code, out, _ = run(["-c", "INSERT INTO users (name) VALUES ('e');  SELECT name FROM users", "--db", str(db), "--echo"])
    assert out.splitlines()[:3] == ["INSERT INTO users (name) VALUES ('e')", "INSERT 0 1", "SELECT name FROM users"]


def test_timing_format(db):
    code, out, _ = run(["-c", "INSERT INTO users (name) VALUES ('t'); SELECT * FROM nope; SELECT name FROM users", "--db", str(db), "--timing", "--on-error", "continue"])
    times = [ln for ln in out.splitlines() if ln.startswith("Time:")]
    assert len(times) == 3  # 오류가 난 문장도 시간을 출력한다 (psql 과 같음)
    for t in times:
        assert re.fullmatch(r"Time: \d+\.\d{3} ms", t)


def test_format_timing_long():
    assert format_timing(0.0003114) == "Time: 0.311 ms"
    assert format_timing(1.2345678) == "Time: 1234.568 ms (00:01.235)"
    assert format_timing(62.5) == "Time: 62500.000 ms (01:02.500)"
    assert format_timing(3725.0) == "Time: 3725000.000 ms (01:02:05.000)"


def test_fsync_option_runs(db):
    code, out, _ = run(["-c", "INSERT INTO users (name) VALUES ('f')", "--db", str(db), "--fsync"])
    assert code == 0 and out == "INSERT 0 1\n"


# ------------------------------------------------------------------ 출력 형식 / dump 연결


def test_explain_output_has_no_tag(db):
    _, out, _ = run(["-c", "EXPLAIN SELECT * FROM users", "--db", str(db)])
    assert out == "    QUERY PLAN     \n-------------------\n Seq Scan on users\n(1 row)\n\n"


def test_dump_subcommand(db, capsys):
    run(["-c", "INSERT INTO users (name) VALUES ('d')", "--db", str(db)])
    assert main(["dump", "users", "--db", str(db)]) == 0
    assert "(1, 'd', NULL)" in capsys.readouterr().out


def test_streaming_large_script_from_stdin(db):
    lines = [f"INSERT INTO users (name, age) VALUES ('n{i}', {i});" for i in range(3000)]
    code, out, _ = run(["run", "-", "--db", str(db)], stdin_text="\n".join(lines) + "\nSELECT id FROM users WHERE age = 2999;")
    assert code == 0
    assert out.endswith("  id  \n------\n 3000\n(1 row)\n\n")
    assert out.count("INSERT 0 1") == 3000


# ------------------------------------------------------------------ --no-index / SET / SHOW


def test_no_index_option(db):
    sql = "INSERT INTO users (name) VALUES ('a'), ('b'); SHOW enable_indexscan; EXPLAIN SELECT * FROM users WHERE id = 2;"
    code, out, err = run(["-c", sql, "--db", str(db)])
    assert (code, err) == (0, "")
    assert " on\n" in out and "Index Scan using users_pkey on users" in out
    code, out, err = run(["-c", "SHOW enable_indexscan; EXPLAIN SELECT * FROM users WHERE id = 2;", "--db", str(db), "--no-index"])
    assert (code, err) == (0, "")
    assert " off\n" in out and "Seq Scan on users" in out and "Index Scan" not in out


def test_no_index_can_be_overridden_by_set(db):
    sql = "SET enable_indexscan = on; EXPLAIN SELECT * FROM users WHERE id = 2;"
    code, out, _ = run(["-c", sql, "--db", str(db), "--no-index"])
    assert code == 0
    assert out.startswith("SET\n") and "Index Scan using users_pkey on users" in out


def test_set_error_output(db):
    code, out, err = run(["-c", "SET enable_indexscan = maybe;", "--db", str(db)])
    assert code == 1 and out == ""
    assert err.splitlines()[0] == 'ERROR:  parameter "enable_indexscan" requires a Boolean value'


def test_set_is_session_scoped_per_run(db):
    run(["-c", "SET enable_indexscan = off;", "--db", str(db)])
    code, out, _ = run(["-c", "SHOW enable_indexscan;", "--db", str(db)])
    assert " on\n" in out  # 실행(세션)마다 기본값에서 시작
