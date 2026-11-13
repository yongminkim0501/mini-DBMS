"""단순 쿼리 ('Q'): 다중 문장, 중간 오류, 빈 쿼리, ErrorResponse 필드, RowDescription, 스트리밍, SET/SHOW."""

import struct

from pgclient import frame, kinds


def test_multiple_statements_each_get_results(pg):
    msgs = pg.simple(
        "INSERT INTO users (name, age) VALUES ('kim', 25), ('lee', NULL); SELECT * FROM users; "
        "INSERT INTO users (name) VALUES ('x') RETURNING id; SET enable_indexscan = off; SHOW enable_indexscan"
    )
    assert kinds(msgs) == "C" + "TDDC" + "TDC" + "C" + "TDC" + "Z"
    tags = [m.tag() for m in msgs if m.kind == "C"]
    assert tags == ["INSERT 0 2", "SELECT 2", "INSERT 0 1", "SET", "SHOW"]
    rows = [m.text_row() for m in msgs if m.kind == "D"]
    assert rows == [["1", "kim", "25"], ["2", "lee", None], ["3"], ["off"]]
    assert msgs[-1].body == b"I"


def test_error_stops_remaining_statements(pg):
    msgs = pg.simple("INSERT INTO users (name) VALUES ('a'); SELECT * FROM nope; INSERT INTO users (name) VALUES ('b')")
    assert kinds(msgs) == "CEZ"
    f = msgs[1].fields()
    assert f["S"] == f["V"] == "ERROR"
    assert (f["C"], f["M"]) == ("42P01", 'relation "nope" does not exist')
    assert pg.rows("SELECT name FROM users") == [["a"]]  # 세 번째 문장은 실행되지 않았다


def test_empty_query(pg):
    assert kinds(pg.simple("")) == "IZ"
    assert kinds(pg.simple("  ;; -- comment\n")) == "IZ"


def test_error_fields_detail_hint_and_position(pg):
    sql = "INSERT INTO users (id) VALUES (5)"
    f = pg.simple(sql)[0].fields()
    assert f["C"] == "428C9"
    assert f["D"] == 'Column "id" is an identity column defined as GENERATED ALWAYS.'
    assert f["P"] == str(sql.index("5") + 1)
    f = pg.simple("SELECT * FROM users WHERE name = 1")[0].fields()
    assert f["H"].startswith("No operator matches")


def test_position_is_characters_and_whole_message(pg):
    # 한글 뒤의 위치: 바이트가 아니라 문자 단위, 메시지 전체(두 번째 문장 포함) 기준
    sql = "SELECT name FROM users WHERE name = '홍길동';\nSELECT * FROM 사용자"
    msgs = pg.simple(sql)
    err = [m for m in msgs if m.kind == "E"][0].fields()
    assert err["M"] == 'relation "사용자" does not exist'
    assert err["P"] == str(sql.index("사용자") + 1)
    assert int(err["P"]) < len(sql.encode("utf-8"))
    f = pg.simple("SELECT '한글' FORM users")[0].fields()
    assert f["P"] == str("SELECT '한글' FORM users".index("'한글'") + 1)


def test_syntax_error_in_second_statement_after_success(pg):
    msgs = pg.simple("SELECT * FROM users; SELEC 1")
    assert kinds(msgs) == "TCEZ"
    assert msgs[2].fields()["P"] == str("SELECT * FROM users; SELEC 1".index("SELEC 1") + 1)


def test_row_description_fields(pg):
    pg.simple("INSERT INTO users (name, age) VALUES ('a', 1)")
    msgs = pg.simple("SELECT age, name, id FROM users")
    assert msgs[0].columns() == [
        ("age", 16384, 3, 20, 8, -1, 0),
        ("name", 16384, 2, 25, -1, -1, 0),
        ("id", 16384, 1, 20, 8, -1, 0),
    ]
    # EXPLAIN, SHOW 는 계산 컬럼: 테이블 OID 0, 컬럼 번호 0
    assert pg.simple("EXPLAIN SELECT * FROM users")[0].columns() == [("QUERY PLAN", 0, 0, 25, -1, -1, 0)]
    assert pg.simple("SHOW datestyle")[0].columns() == [("DateStyle", 0, 0, 25, -1, -1, 0)]


def test_null_is_length_minus_one(pg):
    pg.simple("INSERT INTO users (name, age) VALUES (NULL, NULL)")
    d = [m for m in pg.simple("SELECT name, age FROM users") if m.kind == "D"][0]
    assert d.body == struct.pack("!hii", 2, -1, -1)


def test_command_tags(pg):
    def tag(sql):
        return [m.tag() for m in pg.simple(sql) if m.kind == "C"]

    assert tag("INSERT INTO users (name) VALUES ('a'), ('b')") == ["INSERT 0 2"]
    assert tag("SELECT * FROM users WHERE id = 99") == ["SELECT 0"]
    assert tag("SELECT * FROM users") == ["SELECT 2"]
    assert tag("EXPLAIN SELECT * FROM users") == ["EXPLAIN"]
    assert tag("SET enable_indexscan TO DEFAULT") == ["SET"]
    assert tag("SHOW TimeZone") == ["SHOW"]


def test_large_result_streams(pg):
    n = 20000
    pg.simple("INSERT INTO users (name, age) VALUES " + ", ".join(f"('n{i}', {i})" for i in range(n)))
    msgs = pg.simple("SELECT * FROM users")
    data = [m for m in msgs if m.kind == "D"]
    assert len(data) == n and data[-1].text_row() == [str(n), f"n{n - 1}", str(n - 1)]
    assert msgs[-2].tag() == f"SELECT {n}"


def test_read_only_settings_and_application_name(pg):
    f = pg.simple("SET server_version = '9'")[0].fields()
    assert (f["C"], f["M"]) == ("55P02", 'parameter "server_version" cannot be changed')
    assert pg.rows("SHOW server_version") == [["16.0"]]
    assert pg.rows("SHOW is_superuser") == [["on"]]
    assert pg.rows("SHOW session_authorization") == [["test"]]
    # application_name 이 바뀌면 같은 응답 안에서 ParameterStatus
    msgs = pg.simple("SET application_name = 'reporting'")
    assert kinds(msgs) == "CSZ"
    assert msgs[1].body == b"application_name\x00reporting\x00"
    assert kinds(pg.simple("SET application_name = 'reporting'")) == "CZ"  # 값이 같으면 보내지 않는다
    assert pg.rows("SHOW application_name") == [["reporting"]]
    msgs = pg.simple("SET application_name TO DEFAULT")  # 시작 패킷 값(없으면 빈 문자열)으로
    assert msgs[1].body == b"application_name\x00\x00"


def test_client_encoding_set(pg):
    assert kinds(pg.simple("SET client_encoding = 'utf-8'")) == "CZ"  # 이미 UTF8
    f = pg.simple("SET client_encoding = 'LATIN1'")[0].fields()
    assert (f["C"], f["M"]) == ("22023", 'invalid value for parameter "client_encoding": "LATIN1"')
    assert pg.rows("SHOW client_encoding") == [["UTF8"]]


def test_parameter_in_simple_query_is_undefined(pg):
    f = pg.simple("SELECT * FROM users WHERE id = $1")[0].fields()
    assert (f["C"], f["M"], f["P"]) == ("42P02", "there is no parameter $1", "32")


def test_unsupported_statements_error_naturally(pg):
    # psql 의 \d 류 메타 명령은 pg_catalog 를 조회하므로 자연스럽게 오류가 난다
    f = pg.simple("SELECT c.relname FROM pg_catalog.pg_class c")[0].fields()
    assert f["C"] in ("0A000", "42601", "3F000")
    f = pg.simple("BEGIN")[0].fields()
    assert (f["C"], f["M"]) == ("0A000", "BEGIN statements are not supported")


def test_unknown_message_type_is_fatal(pg):
    pg.send(frame(b"!", b""))
    msg = pg.read()
    f = msg.fields()
    assert (f["S"], f["C"]) == ("FATAL", "08P01")
    assert f["M"] == "invalid frontend message type 33"
    assert pg.is_closed()


def test_invalid_message_length_is_fatal(pg):
    pg.send(b"Q" + struct.pack("!i", 2))
    f = pg.read().fields()
    assert (f["S"], f["C"]) == ("FATAL", "08P01")
    assert pg.is_closed()


def test_small_message_type_too_long_is_fatal(pg):
    pg.send(b"S" + struct.pack("!i", 20000))  # Sync 는 10,000 바이트 제한
    f = pg.read().fields()
    assert f["C"] == "08P01" and "invalid message length" in f["M"]


def test_malformed_query_body(pg):
    pg.send(frame(b"Q", b"SELECT 1"))  # 종료 NUL 없음
    msgs = pg.read_until("Z")
    assert msgs[0].fields()["C"] == "08P01"
    assert pg.rows("SHOW server_version") == [["16.0"]]  # 연결은 유지된다


def test_message_split_across_packets(pg):
    data = frame(b"Q", b"SHOW server_version\x00")
    for i in range(len(data)):
        pg.send(data[i : i + 1])
    assert [m.text_row() for m in pg.read_until("Z") if m.kind == "D"] == [["16.0"]]
