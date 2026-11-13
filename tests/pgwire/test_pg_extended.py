"""확장 쿼리: Parse/Bind/Describe/Execute/Sync/Close/Flush, 에러 후 Sync 까지 무시, PortalSuspended, 바이너리 형식."""

import struct

import pytest
from pgclient import FLUSH, SYNC, bind, close, describe, execute, kinds, parse


def run(pg, *msgs):
    pg.send(b"".join(msgs) + SYNC)
    return pg.read_until("Z")


@pytest.fixture
def seeded(pg):
    pg.simple("INSERT INTO users (name, age) VALUES ('kim', 25), ('홍길동', 31), ('lee', NULL), ('park', 40)")
    return pg


def test_full_cycle_unnamed(seeded):
    msgs = run(
        seeded,
        parse("SELECT id, name FROM users WHERE id = $1"),
        bind((b"2",)),
        describe("P"),
        execute(),
    )
    assert kinds(msgs) == "12TDCZ"
    assert msgs[2].columns()[1] == ("name", 16384, 2, 25, -1, -1, 0)
    assert msgs[3].text_row() == ["2", "홍길동"]
    assert msgs[4].tag() == "SELECT 1"


def test_describe_statement_parameter_types(seeded):
    msgs = run(
        seeded,
        parse("INSERT INTO users (name, age) VALUES ($1, $2) RETURNING id", "ins"),
        describe("S", "ins"),
        parse("SELECT * FROM users WHERE age > $1 AND name = $2 LIMIT $3", "sel"),
        describe("S", "sel"),
        parse("SET enable_indexscan = off", "set"),
        describe("S", "set"),
    )
    assert kinds(msgs) == "1tT" + "1tT" + "1tn" + "Z"
    assert msgs[1].param_oids() == [25, 20]
    assert msgs[2].columns() == [("id", 16384, 1, 20, 8, -1, 0)]
    assert msgs[4].param_oids() == [20, 25, 20]
    assert msgs[7].param_oids() == []


def test_named_statement_reuse_with_different_values(seeded):
    seeded.send(parse("SELECT name FROM users WHERE id = $1", "byid") + SYNC)
    assert kinds(seeded.read_until("Z")) == "1Z"
    for i, name in [(1, "kim"), (4, "park"), (2, "홍길동")]:
        msgs = run(seeded, bind((str(i).encode(),), stmt="byid"), execute())
        assert [m.text_row() for m in msgs if m.kind == "D"] == [[name]]


def test_insert_with_params_null_unicode_bigint(pg):
    big = 2**63 - 1
    msgs = run(
        pg,
        parse("INSERT INTO users (name, age) VALUES ($1, $2) RETURNING *"),
        bind(("한글 'quote'".encode(), str(big).encode())),
        execute(),
        bind((None, None)),
        execute(),
    )
    assert kinds(msgs) == "12DC2DCZ"
    assert msgs[2].text_row() == ["1", "한글 'quote'", str(big)]
    assert msgs[3].tag() == "INSERT 0 1"
    assert msgs[5].text_row() == ["2", None, None]


def test_insert_without_returning_has_no_data(pg):
    msgs = run(pg, parse("INSERT INTO users (name) VALUES ($1)"), bind((b"a",)), describe("P"), execute())
    assert kinds(msgs) == "12nCZ"
    assert msgs[3].tag() == "INSERT 0 1"


def test_portal_suspended_and_resume(seeded):
    msgs = run(
        seeded,
        parse("SELECT id FROM users"),
        bind(portal="cur"),
        execute("cur", 3),
        execute("cur", 3),
    )
    assert kinds(msgs) == "12DDDsDCZ"
    assert [m.text_row() for m in msgs if m.kind == "D"] == [["1"], ["2"], ["3"], ["4"]]
    assert msgs[-2].tag() == "SELECT 1"  # PostgreSQL 처럼 이번 Execute 에서 보낸 행 수


def test_portal_exact_count_suspends_then_completes_empty(seeded):
    msgs = run(seeded, parse("SELECT id FROM users"), bind(portal="p"), execute("p", 4), execute("p", 4))
    assert kinds(msgs) == "12DDDDsCZ"
    assert msgs[-2].tag() == "SELECT 0"


def test_named_portal_survives_across_syncs_unnamed_does_not(seeded):
    seeded.send(parse("SELECT id FROM users") + bind(portal="keep") + execute("keep", 2) + SYNC)
    assert kinds(seeded.read_until("Z")) == "12DDsZ"
    msgs = run(seeded, execute("keep", 0))
    assert [m.text_row() for m in msgs if m.kind == "D"] == [["3"], ["4"]]
    seeded.send(parse("SELECT id FROM users") + bind() + execute("", 1) + SYNC)
    assert kinds(seeded.read_until("Z")) == "12DsZ"
    f = run(seeded, execute("", 1))[0].fields()  # 이름 없는 포털은 Sync 에서 닫혔다
    assert (f["C"], f["M"]) == ("34000", 'portal "" does not exist')


def test_portal_keeps_snapshot_and_does_not_block_writes(seeded, client_factory):
    seeded.send(parse("SELECT id FROM users") + bind(portal="snap") + execute("snap", 1) + SYNC)
    assert kinds(seeded.read_until("Z")) == "12DsZ"
    other = client_factory()
    assert kinds(other.simple("INSERT INTO users (name) VALUES ('new')")) == "CZ"  # 포털이 열려 있어도 쓰기 가능
    msgs = run(seeded, execute("snap", 0))
    assert [m.text_row() for m in msgs if m.kind == "D"] == [["2"], ["3"], ["4"]]  # 새 행(5)은 보이지 않는다


def test_error_discards_until_sync(seeded):
    msgs = run(
        seeded,
        parse("SELECT * FROM nope"),
        bind(),
        describe("P"),
        execute(),
    )
    assert kinds(msgs) == "EZ"  # Bind/Describe/Execute 는 버려졌다
    assert msgs[0].fields()["C"] == "42P01"
    # Sync 이후에는 정상
    msgs = run(seeded, parse("SELECT name FROM users WHERE id = $1"), bind((b"1",)), execute())
    assert kinds(msgs) == "12DCZ"


def test_queries_ignored_until_sync_including_simple_query(seeded):
    seeded.send(parse("SELEC") + bind())
    seeded.send(b"Q" + struct.pack("!i", 4 + 9) + b"SELECT 1\x00")  # Sync 전의 Q 도 버린다
    seeded.send(SYNC)
    msgs = seeded.read_until("Z")
    assert kinds(msgs) == "EZ"


def test_flush_sends_without_sync(seeded):
    seeded.send(parse("SELECT name FROM users WHERE id = $1") + bind((b"1",)) + execute() + FLUSH)
    got = [seeded.read() for _ in range(4)]
    assert kinds(got) == "12DC"
    seeded.send(SYNC)
    assert kinds(seeded.read_until("Z")) == "Z"


def test_multiple_commands_rejected(pg):
    f = run(pg, parse("SELECT * FROM users; SELECT * FROM users"))[0].fields()
    assert (f["C"], f["M"]) == ("42601", "cannot insert multiple commands into a prepared statement")


def test_empty_prepared_statement(pg):
    msgs = run(pg, parse(""), bind(), describe("P"), execute())
    assert kinds(msgs) == "12nIZ"


def test_duplicate_and_missing_names(pg):
    msgs = run(pg, parse("SELECT * FROM users", "s1"), parse("SELECT * FROM users", "s1"))
    assert kinds(msgs) == "1EZ"
    assert msgs[1].fields()["C"] == "42P05"
    assert msgs[1].fields()["M"] == 'prepared statement "s1" already exists'
    # 이름 없는 문장은 덮어쓴다
    assert kinds(run(pg, parse("SELECT * FROM users"), parse("SELECT id FROM users"))) == "11Z"
    f = run(pg, bind(stmt="nope"))[0].fields()
    assert (f["C"], f["M"]) == ("26000", 'prepared statement "nope" does not exist')
    f = run(pg, describe("S", "nope"))[0].fields()
    assert f["C"] == "26000"
    f = run(pg, describe("P", "nope"))[0].fields()
    assert (f["C"], f["M"]) == ("34000", 'portal "nope" does not exist')
    f = run(pg, execute("nope"))[0].fields()
    assert f["C"] == "34000"
    f = run(pg, bind(stmt="s1", portal="p1"), bind(stmt="s1", portal="p1"))[1].fields()
    assert (f["C"], f["M"]) == ("42P03", 'cursor "p1" already exists')


def test_close_statement_and_portal(pg):
    msgs = run(pg, parse("SELECT * FROM users", "c1"), bind(stmt="c1", portal="cp"), close("P", "cp"), close("S", "c1"))
    assert kinds(msgs) == "1233Z"
    assert run(pg, execute("cp"))[0].fields()["C"] == "34000"
    assert run(pg, bind(stmt="c1"))[0].fields()["C"] == "26000"
    assert kinds(run(pg, close("S", "never"), close("P", "never"))) == "33Z"  # 없는 이름 닫기는 오류 아님


def test_bind_parameter_count_mismatch(pg):
    f = run(pg, parse("SELECT * FROM users WHERE id = $1"), bind((b"1", b"2")))[1].fields()
    assert (f["C"], f["M"]) == ("08P01", 'bind message supplies 2 parameters, but prepared statement "" requires 1')
    f = run(pg, parse("SELECT * FROM users WHERE id = $1", "q"), bind(stmt="q"))[1].fields()
    assert f["M"] == 'bind message supplies 0 parameters, but prepared statement "q" requires 1'


def test_bind_bad_format_codes(pg):
    msgs = run(pg, parse("SELECT * FROM users WHERE id = $1"), bind((b"1",), param_formats=(2,)))
    assert msgs[1].fields() | {} and msgs[1].fields()["M"] == "unsupported format code: 2"
    msgs = run(pg, parse("SELECT id, name FROM users"), bind(result_formats=(0, 1, 0)))
    assert msgs[1].fields()["M"] == "bind message has 3 result formats but query has 2 columns"
    msgs = run(pg, parse("SELECT * FROM users WHERE id = $1 AND age = $2"), bind((b"1", b"2"), param_formats=(0, 0, 0)))
    assert msgs[1].fields()["M"] == "bind message has 3 parameter formats but 2 parameters"


def test_bind_value_conversion_errors(pg):
    f = run(pg, parse("SELECT * FROM users WHERE id = $1"), bind((b"abc",)))[1].fields()
    assert (f["C"], f["M"]) == ("22P02", 'invalid input syntax for type bigint: "abc"')
    f = run(pg, parse("SELECT * FROM users WHERE id = $1", oids=(23,)), bind((b"3000000000",)))[1].fields()
    assert (f["C"], f["M"]) == ("22003", 'value "3000000000" is out of range for type integer')
    f = run(pg, parse("SELECT * FROM users WHERE name = $1"), bind((b"\xff",)))[1].fields()
    assert f["C"] == "22021"
    f = run(pg, parse("INSERT INTO users (name) VALUES ($1)"), bind((b"x",), param_formats=(1,)))
    assert kinds(f) == "12Z"  # text 바이너리 = UTF-8 바이트


def test_binary_parameters_and_results(seeded):
    msgs = run(
        seeded,
        parse("SELECT id, name, age FROM users WHERE id = $1", oids=(20,)),
        bind((struct.pack("!q", 2),), param_formats=(1,), result_formats=(1,)),
        describe("P"),
        execute(),
    )
    assert kinds(msgs) == "12TDCZ"
    assert [c[6] for c in msgs[2].columns()] == [1, 1, 1]
    vals = msgs[3].row()
    assert vals[0] == struct.pack("!q", 2)
    assert vals[1] == "홍길동".encode()
    assert vals[2] == struct.pack("!q", 31)
    # int4 / int2 바이너리 파라미터, 컬럼별 결과 형식
    msgs = run(
        seeded,
        parse("SELECT id, name FROM users WHERE id >= $1 AND age <= $2", oids=(23, 21)),
        bind((struct.pack("!i", 1), struct.pack("!h", 30)), param_formats=(1,), result_formats=(0, 1)),
        execute(),
    )
    assert [m.row() for m in msgs if m.kind == "D"] == [[b"1", b"kim"]]
    f = run(seeded, parse("SELECT * FROM users WHERE id = $1", oids=(20,)), bind((b"\x00\x01",), param_formats=(1,)))
    assert (f[1].fields()["C"], f[1].fields()["M"]) == ("22P03", "incorrect binary data format in bind parameter 1")


def test_set_in_extended_reports_parameter_status_at_sync(pg):
    msgs = run(pg, parse("SET application_name = 'ext'"), bind(), execute())
    assert kinds(msgs) == "12CSZ"
    assert msgs[3].body == b"application_name\x00ext\x00"


def test_explain_with_param_uses_index_scan(seeded):
    msgs = run(seeded, parse("EXPLAIN SELECT * FROM users WHERE id = $1"), bind((b"2",)), execute())
    plan = [m.text_row()[0] for m in msgs if m.kind == "D"]
    assert plan == ["Index Scan using users_pkey on users", "  Index Cond: (id = $1)"]
    msgs = run(seeded, parse("EXPLAIN ANALYZE SELECT * FROM users WHERE $1 = id"), bind((b"2",)), execute())
    plan = [m.text_row()[0] for m in msgs if m.kind == "D"]
    assert plan[0].startswith("Index Scan using users_pkey on users  (actual time=") and "rows=1 " in plan[0]


def test_null_parameter_matches_nothing(seeded):
    msgs = run(seeded, parse("SELECT * FROM users WHERE age = $1"), bind((None,)), execute())
    assert kinds(msgs) == "12CZ" and msgs[2].tag() == "SELECT 0"
    msgs = run(seeded, parse("SELECT id FROM users LIMIT $1"), bind((None,)), execute())  # LIMIT NULL = 제한 없음
    assert msgs[-2].tag() == "SELECT 4"
    f = run(seeded, parse("SELECT id FROM users LIMIT $1"), bind((b"-1",)), execute())[1].fields()  # Bind 에서 값 검사
    assert (f["C"], f["M"]) == ("2201W", "LIMIT must not be negative")


def test_not_null_parameter_violation(db_dir, server_factory):
    import json

    from pgclient import PgClient

    (db_dir / "schema" / "16385.json").write_text(
        json.dumps({"oid": 16385, "table": "t", "columns": [
            {"name": "id", "type": "BIGINT", "auto_increment": True}, {"name": "v", "type": "TEXT", "nullable": False}]}),
        encoding="utf-8",
    )  # fmt: skip
    s = server_factory()
    c = PgClient(s.pg_address)
    try:
        c.startup()
        f = run(c, parse("INSERT INTO t (v) VALUES ($1)"), bind((None,)), execute())[1].fields()  # Bind 에서 검사
        assert (f["C"], f["M"]) == ("23502", 'null value in column "v" of relation "t" violates not-null constraint')
    finally:
        c.close()


def test_parse_errors_report_position_in_query(pg):
    sql = "SELECT * FROM users WHERE 이름 = $1"
    f = run(pg, parse(sql))[0].fields()
    assert (f["C"], f["P"]) == ("42703", str(sql.index("이름") + 1))


def test_parameter_type_errors(pg):
    f = run(pg, parse("SELECT * FROM users WHERE id = $2"))[0].fields()
    assert (f["C"], f["M"]) == ("42P18", "could not determine data type of parameter $1")
    f = run(pg, parse("SELECT * FROM users WHERE name = $1", oids=(20,)))[0].fields()
    assert (f["C"], f["M"]) == ("42883", "operator does not exist: text = bigint")
    f = run(pg, parse("SELECT * FROM users WHERE name = $1 AND id = $1"))[0].fields()
    assert (f["C"], f["M"]) == ("42P08", "inconsistent types deduced for parameter $1")
    f = run(pg, parse("SELECT * FROM users WHERE id = $1", oids=(16,)))[0].fields()
    assert f["C"] == "0A000"
