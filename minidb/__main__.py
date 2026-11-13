"""minidb 명령줄 도구.

사용법::

    python -m minidb run <file.sql | -> --db <dir> [--on-error stop|continue] [--echo] [--timing] [--fsync] [--no-index]
    python -m minidb -c "<SQL>" --db <dir> [같은 옵션]
    python -m minidb dump <table> --db <dir> [--limit N]
    python -m minidb serve --db <dir> [--host 127.0.0.1] [--http-port 8080] [--pg-port 5433] [--pg-database minidb]
                           [--workers N] [--queue-size M] [--max-connections K] [--idle-timeout S] [--max-result-rows R]
    python -m minidb client --url http://127.0.0.1:8080 [-c "<SQL>" | file.sql | -]

- 스크립트는 UTF-8 로 읽고 ``-`` 면 표준 입력. 파일 전체를 메모리에 올리지 않고 문장 단위로 스트리밍한다.
- 결과는 psql aligned 형식으로 표준 출력, 오류는 psql 형식(LINE/캐럿/DETAIL/HINT)으로 표준 오류.
- 종료 코드: 모두 성공 0, 문장 오류가 하나라도 있으면 1, 사용법/파일 오류 2.
- 실행 전체가 세션 하나다. ``--no-index`` 는 시작 시 ``SET enable_indexscan = off`` 와 같다
  (스크립트 안의 SET 으로 다시 켤 수 있다).
"""

from __future__ import annotations

import argparse
import io
import sys
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import TextIO

from .engine import Database
from .errors import DatabaseError
from .formatter import format_table
from .result import QueryResult
from .session import Session
from .sql.errorfmt import format_error
from .sql.stream import ScriptStatement, iter_script_statements

__all__ = ["main", "format_timing"]

_DEFAULT_PG_PORT = 5433  # 실제 PostgreSQL(5432)과 겹치지 않게

EXIT_OK = 0
EXIT_STATEMENT_ERROR = 1
EXIT_USAGE = 2


def format_timing(seconds: float) -> str:
    """psql ``\\timing`` 과 같은 형식: ``Time: 0.311 ms``, 1초 이상이면 ``Time: 1234.567 ms (00:01.235)``."""
    ms = seconds * 1000
    text = f"Time: {ms:.3f} ms"
    if ms < 1000:
        return text
    secs = seconds
    hours, rem = divmod(secs, 3600)
    minutes, secs = divmod(rem, 60)
    if hours >= 1:
        return f"{text} ({int(hours):02d}:{int(minutes):02d}:{secs:06.3f})"
    return f"{text} ({int(minutes):02d}:{secs:06.3f})"


def _common_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", required=True, help="DB 디렉터리 (schema/, data/)")
    p.add_argument("--on-error", choices=("stop", "continue"), default="stop", help="문장 오류 시 동작 (기본 stop)")
    p.add_argument("--echo", action="store_true", help="실행 전에 각 문장을 출력 (psql -e)")
    p.add_argument("--timing", action="store_true", help="문장마다 실행 시간 출력 (psql \\timing)")
    p.add_argument("--fsync", action="store_true", help="문장마다 데이터 파일을 fsync")
    p.add_argument(
        "--no-index", action="store_true", help="인덱스 사용 안 함 (시작 시 enable_indexscan = off)"
    )


def _run_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m minidb run", description="SQL 스크립트 파일을 실행한다.")
    p.add_argument("file", help="SQL 파일 경로 (- 는 표준 입력)")
    _common_options(p)
    return p


def _command_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m minidb",
        description="파일 기반 미니 DBMS. 'run <file>' 으로 스크립트를, -c 로 SQL 문자열을, 'dump <table>' 로 테이블 파일을 본다.",
    )
    p.add_argument("-c", dest="command", required=True, metavar="SQL", help="실행할 SQL (여러 문장 가능)")
    _common_options(p)
    return p


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """CLI 진입점. 종료 코드를 반환한다 (테스트에서 입출력 스트림을 바꿔 끼울 수 있다)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    if argv and argv[0] == "dump":
        from .tools.dump import main as dump_main

        return dump_main(argv[1:])
    if argv and argv[0] == "serve":
        return _serve(argv[1:], out, err)
    if argv and argv[0] == "client":
        from .client import main as client_main

        return client_main(argv[1:], stdin=stdin, stdout=out, stderr=err)

    try:
        if argv and argv[0] == "run":
            args = _run_parser().parse_args(argv[1:])
        else:
            args = _command_parser().parse_args(argv)
    except SystemExit as exc:  # argparse 의 사용법 오류 (도움말 -h 는 0)
        return EXIT_USAGE if exc.code not in (0, None) else EXIT_OK

    db_dir = Path(args.db)
    if not db_dir.is_dir():
        print(f'minidb: database directory "{db_dir}" does not exist', file=err)
        return EXIT_USAGE

    # 입력 스트림 열기 (UTF-8, 줄바꿈 변환 없음: 위치 계산이 원문과 일치하도록 newline="")
    reader: TextIO
    close_reader = False
    if getattr(args, "command", None) is not None:
        reader = io.StringIO(args.command)
    elif args.file == "-":
        if stdin is not None:
            reader = stdin
        else:
            reader = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", newline="")
    else:
        try:
            reader = open(args.file, encoding="utf-8", newline="")
            close_reader = True
        except OSError as exc:
            print(f'minidb: could not open file "{args.file}": {exc.strerror}', file=err)
            return EXIT_USAGE

    try:
        try:
            db = Database(db_dir)
        except DatabaseError as exc:
            print(format_error("", exc), file=err)
            return EXIT_USAGE
        with db:
            return _run(db, iter_script_statements(reader), args, out, err)
    except UnicodeDecodeError as exc:
        print(f"minidb: input is not valid UTF-8: {exc}", file=err)
        return EXIT_USAGE
    finally:
        if close_reader:
            reader.close()


def _run(db: Database, statements: Iterator[ScriptStatement], args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    had_error = False
    session = Session(enable_indexscan=not args.no_index)
    for st in statements:
        if args.echo:
            print(st.source.text, file=out)
        t0 = time.perf_counter()
        try:
            result = db.execute_one(st.source, session)
            _print_result(result, out)  # SELECT 결과는 여기서 스트리밍으로 읽힌다 (읽는 중 오류도 잡는다)
        except DatabaseError as exc:
            had_error = True
            out.flush()  # 표준 출력과 표준 오류의 순서를 맞춘다
            print(
                format_error(st.context, exc, position_offset=st.context_offset, line_offset=st.line - 1),
                file=err,
            )
            err.flush()
        if args.timing:
            print(format_timing(time.perf_counter() - t0), file=out)
        if args.fsync:
            db.flush(durable=True)
        if had_error and args.on_error == "stop":
            break
    out.flush()
    return EXIT_STATEMENT_ERROR if had_error else EXIT_OK


def _print_result(result: QueryResult, out: TextIO) -> None:
    """psql 처럼 출력한다: 행이 있는 결과는 표 + 빈 줄, 행이 없는 명령은 태그. RETURNING 은 표 다음에 태그."""
    if result.columns is None:
        print(result.command_tag, file=out)
        return
    lines = format_table(result.columns, result.rows)
    out.write("\n".join(lines) + "\n\n")
    tag = result.command_tag
    if tag.startswith("INSERT"):
        print(tag, file=out)


def _serve(argv: list[str], out: TextIO, err: TextIO) -> int:
    """``serve``: HTTP API 서버 (docs/server.md) + PostgreSQL wire protocol (docs/pg-protocol.md).

    ``--http-port`` 만 주면 HTTP 만, ``--pg-port`` 만 주면 PG 만, 둘 다 주면 둘 다. 둘 다 생략하면 둘 다 기본 포트
    (HTTP 8080, PG 5433 — 실제 PostgreSQL 의 5432 와 겹치지 않게). SIGINT/SIGTERM 으로 정상 종료.
    """
    import os

    from .server import Server, ServerConfig

    d = ServerConfig()
    p = argparse.ArgumentParser(
        prog="python -m minidb serve",
        description=(
            "HTTP API + PostgreSQL wire protocol 서버. 인증이 없으므로(trust) 신뢰할 수 없는 네트워크에 "
            "노출하지 말 것 (기본 127.0.0.1)."
        ),
    )
    p.add_argument("--db", required=True, help="DB 디렉터리 (schema/, data/)")
    p.add_argument("--host", default=d.host)
    p.add_argument(
        "--http-port", "--port", dest="http_port", type=int, default=None,
        help=f"HTTP 포트 (0 이면 임의의 빈 포트). --pg-port 와 둘 다 생략하면 {d.port}",
    )  # fmt: skip
    p.add_argument(
        "--pg-port", type=int, default=None,
        help=f"PostgreSQL 프로토콜 포트 (0 이면 임의). --http-port 와 둘 다 생략하면 {_DEFAULT_PG_PORT}",
    )  # fmt: skip
    p.add_argument("--pg-database", default=d.pg_database, help="PG 접속에서 허용하는 데이터베이스 이름")
    p.add_argument("--workers", type=int, default=os.cpu_count() or d.workers, help="워커 스레드 수 (기본: CPU 코어 수)")
    p.add_argument("--queue-size", type=int, default=d.queue_size, help="대기 큐 길이 (차면 503)")
    p.add_argument("--max-connections", type=int, default=d.max_connections)
    p.add_argument("--idle-timeout", type=float, default=d.idle_timeout, help="유휴 keep-alive 연결 타임아웃 (초)")
    p.add_argument("--request-timeout", type=float, default=d.request_timeout, help="요청 수신 타임아웃 (초)")
    p.add_argument("--max-result-rows", type=int, default=d.max_result_rows)
    p.add_argument("--max-body-bytes", type=int, default=d.max_body_bytes)
    p.add_argument("--session-timeout", type=float, default=d.session_timeout)
    p.add_argument("--lock-timeout", type=float, default=d.lock_timeout, help="락 대기 제한 (초)")
    p.add_argument("--no-access-log", action="store_true", help="요청마다 남기는 접근 로그 끄기")
    try:
        a = p.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code not in (0, None) else EXIT_OK
    if not Path(a.db).is_dir():
        print(f'minidb: database directory "{a.db}" does not exist', file=err)
        return EXIT_USAGE
    http_port, pg_port = a.http_port, a.pg_port
    if http_port is None and pg_port is None:
        http_port, pg_port = d.port, _DEFAULT_PG_PORT
    cfg = ServerConfig(
        host=a.host,
        port=http_port,
        pg_port=pg_port,
        pg_database=a.pg_database,
        workers=a.workers,
        queue_size=a.queue_size,
        max_connections=a.max_connections,
        idle_timeout=a.idle_timeout,
        request_timeout=a.request_timeout,
        max_result_rows=a.max_result_rows,
        max_body_bytes=a.max_body_bytes,
        session_timeout=a.session_timeout,
        lock_timeout=a.lock_timeout,
        access_log=not a.no_access_log,
    )
    try:
        server = Server(a.db, cfg, log_stream=err)
    except DatabaseError as exc:
        print(format_error("", exc), file=err)
        return EXIT_USAGE
    except OSError as exc:
        print(f"minidb: could not listen on {a.host} (http {http_port}, pg {pg_port}): {exc.strerror or exc}", file=err)
        return EXIT_USAGE

    def ready(s: Server) -> None:
        info = s.stats()["runtime"]
        where = []
        if s.address is not None:
            where.append(s.url)
        if s.pg_address is not None:
            host, port = s.pg_address
            where.append(f"postgresql://{host}:{port}/{cfg.pg_database}")
        print(
            f"minidb: listening on {', '.join(where)} (workers={cfg.workers}, queue={cfg.queue_size}, "
            f"python {info['python_version']}, free-threaded={info['free_threaded_build']}, "
            f"GIL={'on' if info['gil_enabled'] else 'off'})",
            file=err,
            flush=True,
        )

    server.serve_forever(ready=ready)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
