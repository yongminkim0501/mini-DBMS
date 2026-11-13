"""``python -m minidb client``: HTTP 서버에 SQL 을 보내고 psql 형식으로 출력하는 클라이언트.

- 입력(-c 문자열, 파일, 표준 입력)을 로컬 CLI 와 같은 스트리밍 분리기로 문장 단위로 나눠, 문장마다 ``POST /query`` 를 보낸다.
  keep-alive 연결 하나와 HTTP 세션 하나(``POST /sessions``)를 쓰므로 ``SET`` 이 다음 문장에도 유지된다.
- 결과는 Phase 1 formatter 로, 오류는 ``format_error`` 로 출력한다. 서버가 돌려준 position 은 문장 기준이므로
  파일 전체 기준으로 옮겨서 ``LINE n:`` 과 캐럿이 로컬 실행과 같게 나온다.
- 종료 코드: 모두 성공 0, 문장 오류 1, 사용법/연결 오류 2 (로컬 CLI 와 같다).
"""

from __future__ import annotations

import argparse
import http.client
import io
import json
import sys
from collections.abc import Sequence
from typing import Any, TextIO
from urllib.parse import urlsplit

from .errors import DatabaseError
from .formatter import format_table
from .result import ColumnInfo, QueryResult
from .sql.errorfmt import format_error
from .sql.stream import iter_script_statements

__all__ = ["main", "MinidbClient"]


class _RemoteError(DatabaseError):
    """서버가 돌려준 SQL 오류 (sqlstate 는 서버 값을 그대로)."""


class MinidbClient:
    """keep-alive 연결 하나로 /query 를 보내는 작은 클라이언트."""

    def __init__(self, url: str, *, timeout: float = 300.0) -> None:
        parts = urlsplit(url)
        if parts.scheme != "http" or not parts.hostname:
            raise ValueError(f"unsupported URL: {url} (use http://host:port)")
        self.host = parts.hostname
        self.port = parts.port or 80
        self.timeout = timeout
        self._conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        self.session_id: str | None = None

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, Any]:
        h = dict(headers or {})
        if self.session_id is not None:
            h.setdefault("X-Session-Id", self.session_id)
        for attempt in (1, 2):
            try:
                self._conn.request(method, path, body=body, headers=h)
                resp = self._conn.getresponse()
                data = resp.read()
                break
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError):
                # 서버가 유휴 keep-alive 연결을 닫았다: 한 번만 다시 연결해서 보낸다
                self._conn.close()
                self._conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
                if attempt == 2:
                    raise
        if resp.getheader("Connection", "").lower() == "close":
            self._conn.close()
        return resp.status, (json.loads(data) if data else None)

    def open_session(self) -> None:
        status, body = self.request("POST", "/sessions")
        if status != 201:
            raise RuntimeError(f"could not create session: HTTP {status}")
        self.session_id = body["session_id"]

    def close_session(self) -> None:
        if self.session_id is not None:
            try:
                self.request("DELETE", f"/sessions/{self.session_id}")
            except OSError:
                pass
            self.session_id = None

    def query(self, sql: str) -> tuple[int, dict[str, Any]]:
        return self.request(
            "POST", "/query", body=sql.encode("utf-8"), headers={"Content-Type": "text/plain; charset=utf-8"}
        )

    def close(self) -> None:
        self.close_session()
        self._conn.close()


def _to_result(r: dict[str, Any]) -> QueryResult:
    if r["columns"] is None:
        return QueryResult(None, [], r["command"])
    cols = [ColumnInfo(c["name"], c["type"], c["type_oid"]) for c in r["columns"]]
    return QueryResult(cols, [tuple(row) for row in r["rows"]], r["command"])


def _to_error(e: dict[str, Any], position_shift: int) -> _RemoteError:
    pos = e.get("position")
    err = _RemoteError(
        e.get("message") or "unknown error",
        sqlstate=e.get("sqlstate") or "XX000",
        position=None if pos is None else pos + position_shift,
        detail=e.get("detail"),
        hint=e.get("hint"),
    )
    return err


def _print_result(result: QueryResult, out: TextIO) -> None:
    if result.columns is None:
        print(result.command_tag, file=out)
        return
    out.write("\n".join(format_table(result.columns, result.rows)) + "\n\n")
    if result.command_tag.startswith("INSERT"):
        print(result.command_tag, file=out)


def main(
    argv: Sequence[str],
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    p = argparse.ArgumentParser(prog="python -m minidb client", description="minidb HTTP 서버에 SQL 을 보낸다.")
    p.add_argument("--url", default="http://127.0.0.1:8080")
    p.add_argument("-c", dest="command", metavar="SQL", help="실행할 SQL")
    p.add_argument("file", nargs="?", help="SQL 파일 (- 는 표준 입력)")
    p.add_argument("--on-error", choices=("stop", "continue"), default="stop")
    try:
        a = p.parse_args(list(argv))
    except SystemExit as exc:
        return 2 if exc.code not in (0, None) else 0
    if (a.command is None) == (a.file is None):
        print("minidb client: give exactly one of -c SQL or a file (- for stdin)", file=err)
        return 2

    close_reader = False
    if a.command is not None:
        reader: TextIO = io.StringIO(a.command)
    elif a.file == "-":
        reader = stdin if stdin is not None else io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", newline="")
    else:
        try:
            reader = open(a.file, encoding="utf-8", newline="")
            close_reader = True
        except OSError as exc:
            print(f'minidb client: could not open file "{a.file}": {exc.strerror}', file=err)
            return 2

    try:
        client = MinidbClient(a.url)
        client.open_session()
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"minidb client: could not connect to {a.url}: {exc}", file=err)
        return 2
    had_error = False
    try:
        for st in iter_script_statements(reader):
            if st.source.error is not None:  # 렉싱 오류는 보내지 않고 바로 보고
                exc: DatabaseError = st.source.error
            else:
                try:
                    status, body = client.query(st.source.text)
                except OSError as e:
                    print(f"minidb client: connection error: {e}", file=err)
                    return 2
                for r in body.get("results") or []:
                    _print_result(_to_result(r), out)
                if body.get("error") is None:
                    continue
                exc = _to_error(body["error"], st.source.start_offset)
            had_error = True
            out.flush()
            print(format_error(st.context, exc, position_offset=st.context_offset, line_offset=st.line - 1), file=err)
            err.flush()
            if a.on_error == "stop":
                break
    except UnicodeDecodeError as e:
        print(f"minidb client: input is not valid UTF-8: {e}", file=err)
        return 2
    finally:
        client.close()
        if close_reader:
            reader.close()
    out.flush()
    return 1 if had_error else 0
