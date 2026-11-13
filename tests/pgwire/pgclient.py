"""테스트 전용 미니 PostgreSQL 클라이언트 (표준 라이브러리 소켓만). 메시지를 바이트 단위로 보내고 받아 검사한다."""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass

PROTOCOL_3_0 = 3 << 16
SSL_REQUEST = 80877103
GSSENC_REQUEST = 80877104
CANCEL_REQUEST = 80877102


def cstr(s: str) -> bytes:
    return s.encode("utf-8") + b"\x00"


def frame(kind: bytes, body: bytes = b"") -> bytes:
    return kind + struct.pack("!i", len(body) + 4) + body


def startup_packet(params: dict[str, str], version: int = PROTOCOL_3_0) -> bytes:
    body = struct.pack("!I", version) + b"".join(cstr(k) + cstr(v) for k, v in params.items()) + b"\x00"
    return struct.pack("!i", len(body) + 4) + body


def query(sql: str) -> bytes:
    return frame(b"Q", cstr(sql))


def parse(sql: str, name: str = "", oids: tuple[int, ...] = ()) -> bytes:
    return frame(b"P", cstr(name) + cstr(sql) + struct.pack("!h", len(oids)) + b"".join(struct.pack("!I", o) for o in oids))


def bind(
    values: tuple[bytes | None, ...] = (),
    *,
    portal: str = "",
    stmt: str = "",
    param_formats: tuple[int, ...] = (),
    result_formats: tuple[int, ...] = (),
) -> bytes:
    body = cstr(portal) + cstr(stmt)
    body += struct.pack("!h", len(param_formats)) + b"".join(struct.pack("!h", f) for f in param_formats)
    body += struct.pack("!h", len(values))
    for v in values:
        body += struct.pack("!i", -1) if v is None else struct.pack("!i", len(v)) + v
    body += struct.pack("!h", len(result_formats)) + b"".join(struct.pack("!h", f) for f in result_formats)
    return frame(b"B", body)


def describe(kind: str, name: str = "") -> bytes:
    return frame(b"D", kind.encode() + cstr(name))


def execute(portal: str = "", max_rows: int = 0) -> bytes:
    return frame(b"E", cstr(portal) + struct.pack("!i", max_rows))


def close(kind: str, name: str = "") -> bytes:
    return frame(b"C", kind.encode() + cstr(name))


SYNC = frame(b"S")
FLUSH = frame(b"H")
TERMINATE = frame(b"X")


@dataclass
class Msg:
    kind: str
    body: bytes

    def fields(self) -> dict[str, str]:
        """ErrorResponse/NoticeResponse 의 필드."""
        out: dict[str, str] = {}
        pos = 0
        while self.body[pos] != 0:
            code = chr(self.body[pos])
            end = self.body.index(b"\x00", pos + 1)
            out[code] = self.body[pos + 1 : end].decode("utf-8")
            pos = end + 1
        return out

    def row(self) -> list[bytes | None]:
        """DataRow 의 값들."""
        (n,) = struct.unpack_from("!h", self.body, 0)
        pos = 2
        vals: list[bytes | None] = []
        for _ in range(n):
            (size,) = struct.unpack_from("!i", self.body, pos)
            pos += 4
            if size == -1:
                vals.append(None)
            else:
                vals.append(self.body[pos : pos + size])
                pos += size
        return vals

    def text_row(self) -> list[str | None]:
        return [None if v is None else v.decode("utf-8") for v in self.row()]

    def columns(self) -> list[tuple[str, int, int, int, int, int, int]]:
        """RowDescription: (이름, 테이블 OID, 컬럼 번호, 타입 OID, 타입 길이, typmod, 형식)."""
        (n,) = struct.unpack_from("!h", self.body, 0)
        pos = 2
        cols = []
        for _ in range(n):
            end = self.body.index(b"\x00", pos)
            name = self.body[pos:end].decode("utf-8")
            pos = end + 1
            toid, attnum, typoid, typlen, typmod, fmt = struct.unpack_from("!IhIhih", self.body, pos)
            pos += 18
            cols.append((name, toid, attnum, typoid, typlen, typmod, fmt))
        return cols

    def tag(self) -> str:
        return self.body[:-1].decode("utf-8")

    def param_oids(self) -> list[int]:
        (n,) = struct.unpack_from("!h", self.body, 0)
        return list(struct.unpack_from(f"!{n}I", self.body, 2))


class PgClient:
    def __init__(self, address: tuple[str, int], timeout: float = 30.0) -> None:
        self.sock = socket.create_connection(address, timeout=timeout)
        self.buf = b""
        self.params: dict[str, str] = {}
        self.backend_key: tuple[int, int] | None = None

    def close(self) -> None:
        self.sock.close()

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def _fill(self, n: int) -> bool:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                return False
            self.buf += chunk
        return True

    def read_byte(self) -> bytes:
        assert self._fill(1), "connection closed"
        b, self.buf = self.buf[:1], self.buf[1:]
        return b

    def read(self) -> Msg | None:
        """메시지 하나 (연결이 닫혔으면 None)."""
        if not self._fill(5):
            return None
        kind = chr(self.buf[0])
        (length,) = struct.unpack_from("!i", self.buf, 1)
        assert self._fill(1 + length), "connection closed mid-message"
        body = self.buf[5 : 1 + length]
        self.buf = self.buf[1 + length :]
        return Msg(kind, body)

    def read_until(self, kind: str = "Z") -> list[Msg]:
        out = []
        while True:
            msg = self.read()
            assert msg is not None, f"connection closed before {kind!r}: {out}"
            out.append(msg)
            if msg.kind == kind:
                return out

    def is_closed(self) -> bool:
        try:
            return self.read() is None
        except (ConnectionResetError, TimeoutError):
            return True

    def startup(self, user: str = "test", database: str | None = "minidb", **extra: str) -> list[Msg]:
        params = {"user": user}
        if database is not None:
            params["database"] = database
        params.update(extra)
        self.send(startup_packet(params))
        msgs = self.read_until("Z")
        for msg in msgs:
            if msg.kind == "S":
                k, v, _ = msg.body.split(b"\x00")
                self.params[k.decode()] = v.decode()
            elif msg.kind == "K":
                self.backend_key = struct.unpack("!II", msg.body)
        return msgs

    def simple(self, sql: str) -> list[Msg]:
        self.send(query(sql))
        return self.read_until("Z")

    def rows(self, sql: str) -> list[list[str | None]]:
        msgs = self.simple(sql)
        errors = [m.fields() for m in msgs if m.kind == "E"]
        assert not errors, errors
        return [m.text_row() for m in msgs if m.kind == "D"]


def kinds(msgs: list[Msg]) -> str:
    return "".join(m.kind for m in msgs)


def cancel_packet(pid: int, key: int) -> bytes:
    return struct.pack("!iIII", 16, CANCEL_REQUEST, pid, key)
