"""시작 단계: SSL/GSS 거절, 버전 협상, user/database/인코딩 검사, ParameterStatus, BackendKeyData."""

import struct

from pgclient import GSSENC_REQUEST, SSL_REQUEST, PgClient, frame, kinds, startup_packet


def test_startup_sequence(client_factory):
    c = client_factory(startup=False)
    msgs = c.startup(user="kim", application_name="myapp")
    assert msgs[0].kind == "R" and msgs[0].body == struct.pack("!i", 0)  # AuthenticationOk (trust)
    assert kinds(msgs) == "R" + "S" * 11 + "KZ"
    assert msgs[-1].body == b"I"
    assert c.params == {
        "application_name": "myapp",
        "client_encoding": "UTF8",
        "server_version": "16.0",
        "server_encoding": "UTF8",
        "DateStyle": "ISO, MDY",
        "integer_datetimes": "on",
        "standard_conforming_strings": "on",
        "TimeZone": "UTC",
        "IntervalStyle": "postgres",
        "is_superuser": "on",
        "session_authorization": "kim",
    }
    pid, key = c.backend_key
    assert pid > 0 and 0 <= key < 2**32


def test_secret_keys_differ(client_factory):
    a, b = client_factory(), client_factory()
    assert a.backend_key[0] != b.backend_key[0]
    assert a.backend_key[1] != b.backend_key[1]


def test_ssl_and_gss_requests_refused_then_plain_startup(client_factory):
    c = client_factory(startup=False)
    c.send(struct.pack("!iI", 8, SSL_REQUEST))
    assert c.read_byte() == b"N"
    c.send(struct.pack("!iI", 8, GSSENC_REQUEST))
    assert c.read_byte() == b"N"
    msgs = c.startup()
    assert msgs[-1].kind == "Z"
    assert c.rows("SHOW server_version") == [["16.0"]]


def fatal(c: PgClient, packet: bytes) -> dict[str, str]:
    c.send(packet)
    msg = c.read()
    assert msg is not None and msg.kind == "E"
    f = msg.fields()
    assert f["S"] == f["V"] == "FATAL"
    assert c.is_closed()
    return f


def test_unsupported_major_version(client_factory):
    f = fatal(client_factory(startup=False), startup_packet({"user": "x"}, version=(2 << 16)))
    assert f["C"] == "0A000"
    assert f["M"] == "unsupported frontend protocol 2.0: server supports 3.0 to 3.0"


def test_minor_version_and_pq_options_negotiated(client_factory):
    c = client_factory(startup=False)
    c.send(startup_packet({"user": "x", "database": "minidb", "_pq_.foo": "1"}, version=(3 << 16) | 2))
    msgs = c.read_until("Z")
    assert msgs[0].kind == "v"
    newest, n = struct.unpack_from("!ii", msgs[0].body)
    assert (newest, n) == (0, 1)
    assert msgs[0].body[8:] == b"_pq_.foo\x00"
    assert msgs[1].kind == "R"


def test_user_required(client_factory):
    f = fatal(client_factory(startup=False), startup_packet({"database": "minidb"}))
    assert (f["C"], f["M"]) == ("28000", "no PostgreSQL user name specified in startup packet")


def test_wrong_database(client_factory):
    f = fatal(client_factory(startup=False), startup_packet({"user": "kim", "database": "other"}))
    assert (f["C"], f["M"]) == ("3D000", 'database "other" does not exist')


def test_database_defaults_to_user_name(client_factory, server_factory):
    f = fatal(client_factory(startup=False), startup_packet({"user": "kim"}))
    assert f["M"] == 'database "kim" does not exist'
    s = server_factory(pg_database="kim")
    c = PgClient(s.pg_address)
    try:
        assert c.startup(user="kim", database=None)[-1].kind == "Z"
    finally:
        c.close()


def test_client_encoding(client_factory):
    for enc in ("UTF8", "utf-8", "Unicode"):
        c = client_factory(client_encoding=enc)
        assert c.params["client_encoding"] == "UTF8"
    f = fatal(client_factory(startup=False), startup_packet({"user": "x", "database": "minidb", "client_encoding": "LATIN1"}))
    assert (f["C"], f["M"]) == ("22023", 'invalid value for parameter "client_encoding": "LATIN1"')


def test_bad_startup_packets(client_factory):
    c = client_factory(startup=False)
    c.send(struct.pack("!iI", 20000, 3 << 16))  # 10KB 초과
    msg = c.read()
    assert msg.fields()["C"] == "08P01" and msg.fields()["M"] == "invalid length of startup packet"
    assert c.is_closed()
    c = client_factory(startup=False)
    c.send(struct.pack("!iI", 12, 3 << 16) + b"user")  # 종료 바이트 없음
    assert c.read().fields()["M"] == "invalid startup packet layout: expected terminator as last byte"


def test_startup_packet_in_pieces(client_factory):
    c = client_factory(startup=False)
    pkt = startup_packet({"user": "kim", "database": "minidb"})
    for i in range(len(pkt)):  # 한 바이트씩: 디스패처가 완전한 패킷이 될 때까지 모은다
        c.send(pkt[i : i + 1])
    assert c.read_until("Z")[-1].body == b"I"


def test_terminate_closes(pg):
    pg.send(frame(b"X"))
    assert pg.is_closed()
