"""pg8000 (순수 파이썬 PostgreSQL 드라이버) 통합 테스트.

C 확장 드라이버(psycopg2 등)는 3.14t 에서 GIL 을 다시 켤 수 있으므로 pytest 프로세스에서 import 하지 않는다
(scripts/compat/check_psycopg.py 를 별도 프로세스로 실행). pg8000 을 import 한 뒤에도 GIL 이 꺼져 있는지 먼저 확인한다.
"""

import sys
import sysconfig
import threading

import pytest

pg8000 = pytest.importorskip("pg8000")
import pg8000.dbapi  # noqa: E402
import pg8000.native  # noqa: E402


def test_pg8000_import_keeps_gil_disabled():
    if not sysconfig.get_config_var("Py_GIL_DISABLED"):
        pytest.skip("GIL build")
    assert sys._is_gil_enabled() is False


@pytest.fixture
def conn(server):
    host, port = server.pg_address
    c = pg8000.native.Connection("tester", host=host, port=port, database="minidb", application_name="pytest")
    yield c
    c.close()


def test_connect_and_parameter_status(conn):
    assert conn.parameter_statuses["server_version"] == "16.0"
    assert conn.parameter_statuses["application_name"] == "pytest"
    assert conn.run("SHOW session_authorization") == [["tester"]]


def test_insert_returning_and_select_with_params(conn):
    rows = conn.run(
        "INSERT INTO users (name, age) VALUES (:n, :a) RETURNING id, name, age", n="홍길동 'q'", a=9_223_372_036_854_775_807
    )
    assert rows == [[1, "홍길동 'q'", 9_223_372_036_854_775_807]]
    conn.run("INSERT INTO users (name, age) VALUES (:n, :a)", n=None, a=None)
    assert conn.row_count == 1
    assert conn.run("SELECT * FROM users WHERE id = :i", i=2) == [[2, None, None]]
    assert conn.run("SELECT name FROM users WHERE age = :a", a=9_223_372_036_854_775_807) == [["홍길동 'q'"]]
    assert conn.run("SELECT id FROM users WHERE id >= :lo LIMIT :n", lo=1, n=1) == [[1]]
    assert [c["name"] for c in conn.columns] == ["id"]
    assert conn.columns[0]["type_oid"] == 20


def test_errors_carry_sqlstate(conn):
    with pytest.raises(pg8000.native.DatabaseError) as ei:
        conn.run("SELECT * FROM nope")
    err = ei.value.args[0]
    assert (err["C"], err["M"], err["P"]) == ("42P01", 'relation "nope" does not exist', "15")
    with pytest.raises(pg8000.native.DatabaseError) as ei:
        conn.run("SELECT * FROM users WHERE id = :x", x="abc")
    assert ei.value.args[0]["C"] == "22P02"
    with pytest.raises(pg8000.native.DatabaseError) as ei:
        conn.run("INSERT INTO users (id) VALUES (1)")
    assert ei.value.args[0]["D"] == 'Column "id" is an identity column defined as GENERATED ALWAYS.'
    assert conn.run("SHOW server_version") == [["16.0"]]  # 오류 뒤에도 연결 사용 가능


def test_set_show(conn):
    conn.run("SET enable_indexscan = off")
    assert conn.run("SHOW enable_indexscan") == [["off"]]
    plan = conn.run("EXPLAIN SELECT * FROM users WHERE id = :i", i=1)
    assert plan[0] == ["Seq Scan on users"]
    conn.run("SET application_name = 'changed'")
    assert conn.parameter_statuses["application_name"] == "changed"  # ParameterStatus 를 받았다


def test_prepared_statement(conn):
    ps = conn.prepare("INSERT INTO users (name, age) VALUES (:n, :a) RETURNING id")
    ids = [ps.run(n=f"p{i}", a=i)[0][0] for i in range(5)]
    assert ids == [1, 2, 3, 4, 5]
    ps.close()


def test_dbapi_autocommit(server):
    host, port = server.pg_address
    c = pg8000.dbapi.connect(user="tester", host=host, port=port, database="minidb")
    c.autocommit = True  # 트랜잭션(BEGIN)은 지원하지 않는다 (docs/pg-protocol.md)
    try:
        cur = c.cursor()
        cur.execute("INSERT INTO users (name, age) VALUES (%s, %s)", ("kim", 25))
        cur.execute("SELECT name, age FROM users WHERE name = %s", ("kim",))
        assert list(cur.fetchall()) == [["kim", 25]]
        assert [d[0] for d in cur.description] == ["name", "age"]
    finally:
        c.close()


def test_concurrent_connections(server):
    host, port = server.pg_address
    errors = []

    def work(i):
        try:
            c = pg8000.native.Connection("t", host=host, port=port, database="minidb")
            for j in range(30):
                rid = c.run("INSERT INTO users (name, age) VALUES (:n, :a) RETURNING id", n=f"w{i}", a=j)[0][0]
                assert c.run("SELECT age FROM users WHERE id = :i", i=rid) == [[j]]
            c.close()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == []
    c = pg8000.native.Connection("t", host=host, port=port, database="minidb")
    assert len(c.run("SELECT id FROM users")) == 240
    c.close()
