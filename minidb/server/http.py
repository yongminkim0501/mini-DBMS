"""HTTP/1.1 요청 파서와 응답 생성 (http.server 를 쓰지 않고 직접 구현, RFC 9110/9112 의 부분집합).

- 요청 줄 ``METHOD SP request-target SP HTTP/1.x``, 헤더(이름은 대소문자 무시), ``Content-Length`` 본문.
- keep-alive: HTTP/1.1 은 기본 유지(``Connection: close`` 면 닫음), HTTP/1.0 은 기본 닫음(``Connection: keep-alive`` 면 유지).
- 요청의 ``Transfer-Encoding`` (chunked 등)은 501. 본문이 필요한 메서드(POST/PUT/PATCH)에 길이가 없으면 411.
- 한계: 요청 줄+헤더 ``max_header_bytes``(기본 16 KiB, 넘으면 431), 본문 ``max_body_bytes``(기본 10 MiB, 넘으면 413).

파서는 순수 함수다. 연결의 수신 버퍼(bytearray)를 받아 완전한 요청이 있으면 그 요청과 소비한 바이트 수를,
아직 덜 왔으면 None 을 돌려준다. 그래서 한 번에 두 요청이 들어온 경우(파이프라이닝)에도 버퍼 앞에서부터 차례로 꺼낼 수 있다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

__all__ = [
    "HttpError",
    "Request",
    "parse_request",
    "build_response",
    "REASONS",
    "MAX_HEADER_BYTES",
    "MAX_BODY_BYTES",
]

MAX_HEADER_BYTES = 16 * 1024
MAX_BODY_BYTES = 10 * 1024 * 1024

REASONS: dict[int, str] = {
    200: "OK",
    201: "Created",
    204: "No Content",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    411: "Length Required",
    413: "Content Too Large",
    415: "Unsupported Media Type",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    501: "Not Implemented",
    503: "Service Unavailable",
    505: "HTTP Version Not Supported",
}

# RFC 9110 token: 메서드와 헤더 이름
_TOKEN_RE = re.compile(rb"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")
_VERSION_RE = re.compile(rb"HTTP/(\d)\.(\d)\Z")
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})


class HttpError(Exception):
    """요청을 처리할 수 없는 프로토콜 오류. 응답 후 연결을 닫는다."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(slots=True)
class Request:
    method: str
    target: str
    path: str
    query: str
    version: tuple[int, int]
    headers: dict[str, str] = field(default_factory=dict)  # 이름은 소문자, 같은 이름이 여럿이면 ", " 로 합침
    body: bytes = b""
    keep_alive: bool = True

    def header(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name.lower(), default)

    @property
    def content_type(self) -> str | None:
        """미디어 타입만 (파라미터 제외, 소문자). 없으면 None."""
        v = self.headers.get("content-type")
        if v is None:
            return None
        return v.split(";", 1)[0].strip().lower()

    @property
    def charset(self) -> str | None:
        v = self.headers.get("content-type") or ""
        for part in v.split(";")[1:]:
            k, _, val = part.strip().partition("=")
            if k.strip().lower() == "charset":
                return val.strip().strip('"').lower()
        return None


def _connection_tokens(headers: dict[str, str]) -> set[str]:
    return {t.strip().lower() for t in headers.get("connection", "").split(",") if t.strip()}


def parse_request(
    buf: bytes | bytearray,
    *,
    max_header_bytes: int = MAX_HEADER_BYTES,
    max_body_bytes: int = MAX_BODY_BYTES,
) -> tuple[Request, int] | None:
    """버퍼 앞의 요청 하나를 해석한다.

    Returns:
        ``(요청, 소비한 바이트 수)`` 또는 아직 완전하지 않으면 None.

    Raises:
        HttpError: 400/411/413/431/501/505.
    """
    # 요청 사이의 빈 줄은 무시한다 (RFC 9112 2.2: 서버는 요청 줄 앞의 CRLF 를 적어도 하나 무시해야 한다)
    start = 0
    while buf[start : start + 2] == b"\r\n":
        start += 2
    end = buf.find(b"\r\n\r\n", start)
    if end < 0:
        if len(buf) - start > max_header_bytes:
            raise HttpError(431, "request header section too large")
        return None
    head_len = end + 4 - start
    if head_len > max_header_bytes:
        raise HttpError(431, "request header section too large")
    lines = bytes(buf[start:end]).split(b"\r\n")

    # ---- 요청 줄
    parts = lines[0].split(b" ")
    if len(parts) != 3 or not all(parts):
        raise HttpError(400, "malformed request line")
    raw_method, raw_target, raw_version = parts
    if not _TOKEN_RE.match(raw_method):
        raise HttpError(400, "invalid method")
    m = _VERSION_RE.match(raw_version)
    if m is None:
        raise HttpError(400, "invalid HTTP version")
    version = (int(m.group(1)), int(m.group(2)))
    if version[0] != 1:
        raise HttpError(505, "only HTTP/1.x is supported")
    try:
        target = raw_target.decode("ascii")
    except UnicodeDecodeError:
        raise HttpError(400, "request target must be ASCII") from None
    if not target.startswith("/") and target != "*":
        raise HttpError(400, "only origin-form request targets are supported")
    split = urlsplit(target)
    method = raw_method.decode("ascii")

    # ---- 헤더
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if line[:1] in (b" ", b"\t"):
            raise HttpError(400, "obsolete header line folding is not allowed")
        name, sep, value = line.partition(b":")
        if not sep or not _TOKEN_RE.match(name):  # 이름 뒤 공백도 여기서 걸린다 (RFC 9112 5.1)
            raise HttpError(400, "malformed header line")
        try:
            v = value.strip(b" \t").decode("latin-1")
        except UnicodeDecodeError:  # pragma: no cover - latin-1 은 실패하지 않는다
            raise HttpError(400, "invalid header value") from None
        if any(c in v for c in "\r\n\x00"):
            raise HttpError(400, "invalid header value")
        key = name.decode("ascii").lower()
        headers[key] = f"{headers[key]}, {v}" if key in headers else v

    if version >= (1, 1) and "host" not in headers:
        raise HttpError(400, "missing Host header")

    # ---- 본문 길이
    if "transfer-encoding" in headers:
        raise HttpError(501, "Transfer-Encoding is not supported; send Content-Length")
    length = 0
    cl = headers.get("content-length")
    if cl is not None:
        values = {x.strip() for x in cl.split(",")}
        if len(values) != 1:
            raise HttpError(400, "conflicting Content-Length values")
        (v,) = values
        if not v.isdigit() or not v.isascii():
            raise HttpError(400, "invalid Content-Length")
        length = int(v)
        if length > max_body_bytes:
            raise HttpError(413, f"request body exceeds {max_body_bytes} bytes")
    elif method in _BODY_METHODS:
        raise HttpError(411, "Content-Length required")

    body_start = start + head_len
    if len(buf) < body_start + length:
        return None
    body = bytes(buf[body_start : body_start + length])

    tokens = _connection_tokens(headers)
    keep_alive = "keep-alive" in tokens if version == (1, 0) else "close" not in tokens
    req = Request(
        method=method,
        target=target,
        path=unquote(split.path),
        query=split.query,
        version=version,
        headers=headers,
        body=body,
        keep_alive=keep_alive,
    )
    return req, body_start + length


def build_response(
    status: int,
    body: bytes = b"",
    *,
    content_type: str | None = "application/json; charset=utf-8",
    keep_alive: bool = True,
    headers: dict[str, str] | None = None,
    head_only: bool = False,
) -> bytes:
    """응답 바이트 (항상 Content-Length 를 붙인다; HEAD 면 본문 생략)."""
    reason = REASONS.get(status, "Unknown")
    lines = [f"HTTP/1.1 {status} {reason}"]
    if content_type is not None and (body or status not in (204,)):
        lines.append(f"Content-Type: {content_type}")
    lines.append(f"Content-Length: {len(body)}")
    lines.append("Connection: keep-alive" if keep_alive else "Connection: close")
    for k, v in (headers or {}).items():
        lines.append(f"{k}: {v}")
    head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
    return head if head_only else head + body
