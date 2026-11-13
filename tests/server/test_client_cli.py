"""python -m minidb client / serve 인자 처리."""

import io
import json

import pytest

from minidb.__main__ import main
from minidb.server import Server, ServerConfig

USERS = {
    "oid": 16384,
    "table": "users",
    "columns": [
        {"name": "id", "type": "BIGINT", "auto_increment": True},
        {"name": "name", "type": "TEXT", "nullable": True},
        {"name": "age", "type": "BIGINT", "nullable": True},
    ],
}


@pytest.fixture
def server(tmp_path):
    (tmp_path / "db" / "schema").mkdir(parents=True)
    (tmp_path / "db" / "schema" / "16384.json").write_text(json.dumps(USERS), encoding="utf-8")
    s = Server(tmp_path / "db", ServerConfig(port=0, workers=2), log_stream=io.StringIO()).start()
    yield s
    s.shutdown(timeout=10)


def run(args, stdin_text=None):
    out, err = io.StringIO(), io.StringIO()
    stdin = io.StringIO(stdin_text) if stdin_text is not None else None
    code = main(args, stdin=stdin, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def test_client_prints_like_psql(server):
    code, out, err = run(["client", "--url", server.url, "-c",
                          "INSERT INTO users (name, age) VALUES ('kim', 25), ('홍길동', 31) RETURNING *"])  # fmt: skip
    assert (code, err) == (0, "")
    assert out == (
        " id |  name  | age \n"
        "----+--------+-----\n"
        "  1 | kim    |  25\n"
        "  2 | 홍길동 |  31\n"
        "(2 rows)\n\nINSERT 0 2\n"
    )
    code, out, _ = run(["client", "--url", server.url, "-c", "SELECT name FROM users WHERE id = 2"])
    assert out == "  name  \n--------\n 홍길동\n(1 row)\n\n"


def test_client_session_keeps_set_across_statements(server):
    sql = "SET enable_indexscan = off;\nEXPLAIN SELECT * FROM users WHERE id = 1;"
    code, out, _ = run(["client", "--url", server.url, "-"], stdin_text=sql)
    assert code == 0
    assert out.startswith("SET\n") and "Seq Scan on users" in out


def test_client_error_format_matches_local(server, tmp_path):
    script = tmp_path / "s.sql"
    script.write_text("INSERT INTO users (name) VALUES ('a');\n\nSELECT *\n  FROM users WHERE name = 1;\n", encoding="utf-8")
    code, out, err = run(["client", "--url", server.url, str(script)])
    assert code == 1
    assert out == "INSERT 0 1\n"
    assert err == (
        "ERROR:  operator does not exist: text = integer\n"
        "LINE 4:   FROM users WHERE name = 1;\n"
        "                                ^\n"
        "HINT:  No operator matches the given name and argument types. You might need to add explicit type casts.\n"
    )
    # 로컬 run 과 같은 출력
    db = server.db.db_dir
    _, _, local_err = run(["run", str(script), "--db", str(db)])
    assert local_err == err


def test_client_lex_error_not_sent(server):
    code, _, err = run(["client", "--url", server.url, "-c", "SELECT 'oops"])
    assert code == 1 and "unterminated quoted string" in err


def test_client_usage_and_connection_errors(server):
    assert run(["client", "--url", server.url])[0] == 2  # -c 도 파일도 없음
    assert run(["client", "--url", server.url, "-c", "x", "f.sql"])[0] == 2
    code, _, err = run(["client", "--url", "http://127.0.0.1:1", "-c", "SELECT * FROM users"])
    assert code == 2 and "could not connect" in err
    assert run(["client", "--url", "ftp://x", "-c", "SELECT 1"])[0] == 2


def test_serve_usage_errors(tmp_path):
    assert run(["serve"])[0] == 2
    code, _, err = run(["serve", "--db", str(tmp_path / "missing")])
    assert code == 2 and "does not exist" in err
