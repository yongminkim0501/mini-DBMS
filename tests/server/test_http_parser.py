"""HTTP/1.1 요청 파서와 응답 생성."""

import pytest

from minidb.server.http import HttpError, build_response, parse_request


def req(text: str) -> bytes:
    return text.replace("\n", "\r\n").encode("latin-1")


def parse(raw, **kw):
    out = parse_request(bytearray(raw), **kw)
    assert out is not None
    return out


def test_simple_get():
    r, used = parse(req("GET /health HTTP/1.1\nHost: x\n\n"))
    assert (r.method, r.path, r.version, r.body, r.keep_alive) == ("GET", "/health", (1, 1), b"", True)
    assert used == len(req("GET /health HTTP/1.1\nHost: x\n\n"))


def test_post_with_body_and_case_insensitive_headers():
    raw = req("POST /query?x=1 HTTP/1.1\nHOST: a\nContent-TYPE: text/plain; charset=UTF-8\nContent-Length: 8\n\n") + b"SELECT 1"
    r, used = parse(raw)
    assert r.body == b"SELECT 1" and used == len(raw)
    assert r.header("content-type") == "text/plain; charset=UTF-8"
    assert r.content_type == "text/plain" and r.charset == "utf-8"
    assert r.query == "x=1" and r.path == "/query"


def test_percent_decoding_in_path():
    r, _ = parse(req("DELETE /sessions/a%20b HTTP/1.1\nHost: x\n\n"))
    assert r.path == "/sessions/a b"


def test_incomplete_returns_none():
    assert parse_request(bytearray(b"GET / HTTP/1.1\r\nHost: x\r\n")) is None
    raw = req("POST /query HTTP/1.1\nHost: x\nContent-Length: 10\n\n") + b"12345"
    assert parse_request(bytearray(raw)) is None


def test_pipelining_two_requests_in_one_buffer():
    a = req("POST /query HTTP/1.1\nHost: x\nContent-Length: 3\n\n") + b"abc"
    b = req("GET /health HTTP/1.1\nHost: x\nConnection: close\n\n")
    buf = bytearray(a + b)
    r1, used1 = parse(buf)
    assert r1.body == b"abc"
    del buf[:used1]
    r2, used2 = parse(buf)
    assert r2.path == "/health" and not r2.keep_alive and used2 == len(b)


@pytest.mark.parametrize(
    ("version", "connection", "keep"),
    [
        ("HTTP/1.1", None, True),
        ("HTTP/1.1", "close", False),
        ("HTTP/1.1", "Keep-Alive, Close", False),
        ("HTTP/1.0", None, False),
        ("HTTP/1.0", "keep-alive", True),
    ],
)
def test_keep_alive_rules(version, connection, keep):
    head = f"GET / {version}\nHost: x\n" + (f"Connection: {connection}\n" if connection else "") + "\n"
    r, _ = parse(req(head))
    assert r.keep_alive is keep


def test_leading_crlf_ignored():
    r, used = parse(b"\r\n\r\n" + req("GET / HTTP/1.1\nHost: x\n\n"))
    assert r.path == "/"


@pytest.mark.parametrize(
    ("raw", "status"),
    [
        (req("GET /\n\n"), 400),  # 요청 줄 모양
        (req("GET  / HTTP/1.1\nHost: x\n\n"), 400),  # 빈 토큰
        (req("G@T / HTTP/1.1\nHost: x\n\n"), 400),
        (req("GET / HTTP/2.0\nHost: x\n\n"), 505),
        (req("GET / HTTX/1.1\nHost: x\n\n"), 400),
        (req("GET http://a/ HTTP/1.1\nHost: x\n\n"), 400),  # absolute-form 미지원
        (req("GET / HTTP/1.1\n\n"), 400),  # Host 없음
        (req("GET / HTTP/1.1\nHost x\n\n"), 400),  # 콜론 없음
        (req("GET / HTTP/1.1\nHost : x\n\n"), 400),  # 이름 뒤 공백
        (req("GET / HTTP/1.1\nHost: x\n folded\n\n"), 400),  # obs-fold
        (req("POST /query HTTP/1.1\nHost: x\nContent-Length: abc\n\n"), 400),
        (req("POST /query HTTP/1.1\nHost: x\nContent-Length: +5\n\n"), 400),
        (req("POST /query HTTP/1.1\nHost: x\nContent-Length: 1\nContent-Length: 2\n\n"), 400),
        (req("POST /query HTTP/1.1\nHost: x\n\n"), 411),
        (req("POST /query HTTP/1.1\nHost: x\nTransfer-Encoding: chunked\n\n"), 501),
    ],
)
def test_bad_requests(raw, status):
    with pytest.raises(HttpError) as ei:
        parse_request(bytearray(raw))
    assert ei.value.status == status


def test_same_content_length_twice_is_ok():
    r, _ = parse(req("POST /q HTTP/1.1\nHost: x\nContent-Length: 2\nContent-Length: 2\n\n") + b"ab")
    assert r.body == b"ab"


def test_header_size_limit():
    big = "X-Big: " + "a" * 20000
    with pytest.raises(HttpError) as ei:
        parse_request(bytearray(req(f"GET / HTTP/1.1\nHost: x\n{big}\n\n")))
    assert ei.value.status == 431
    # 끝(빈 줄)이 아직 안 왔어도 한계를 넘으면 바로 431 (무한히 쌓지 않는다)
    with pytest.raises(HttpError) as ei:
        parse_request(bytearray(req(f"GET / HTTP/1.1\nHost: x\n{big}")))
    assert ei.value.status == 431
    r, _ = parse(req(f"GET / HTTP/1.1\nHost: x\nX-Small: {'a' * 100}\n\n"), max_header_bytes=1024)
    assert r.header("x-small") == "a" * 100


def test_body_size_limit():
    raw = req("POST /query HTTP/1.1\nHost: x\nContent-Length: 11\n\n")
    with pytest.raises(HttpError) as ei:
        parse_request(bytearray(raw), max_body_bytes=10)
    assert ei.value.status == 413  # 본문을 받기 전에 헤더만 보고 거부


def test_repeated_headers_joined():
    r, _ = parse(req("GET / HTTP/1.1\nHost: x\nAccept: a\nAccept: b\n\n"))
    assert r.header("accept") == "a, b"


def test_build_response():
    out = build_response(200, b'{"a":1}', keep_alive=True, headers={"Retry-After": "1"})
    head, body = out.split(b"\r\n\r\n", 1)
    lines = head.decode().split("\r\n")
    assert lines[0] == "HTTP/1.1 200 OK"
    assert "Content-Length: 7" in lines and "Connection: keep-alive" in lines and "Retry-After: 1" in lines
    assert body == b'{"a":1}'
    out = build_response(204, b"", content_type=None, keep_alive=False)
    assert out.startswith(b"HTTP/1.1 204 No Content\r\n") and b"Connection: close" in out
    assert build_response(200, b"xyz", head_only=True).endswith(b"\r\n\r\n")
