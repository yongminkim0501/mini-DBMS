"""테이블 데이터 파일을 사람이 읽을 수 있게 출력하는 도구.

사용법::

    python -m minidb.tools.dump <table> --db <db_dir> [--limit N]

테이블 인자는 SQL 식별자와 같은 규칙으로 해석한다: 큰따옴표로 감싸면 그대로(``'"Users"'``),
아니면 ASCII 대문자만 소문자로 접는다(``Users`` → ``users``). 셸에서는 큰따옴표가 사라지지 않게
작은따옴표로 한 번 더 감싼다.

파일을 절대 수정하지 않는다. 레코드 목록은 :func:`~minidb.storage.inspect_records` 로
CRC 를 하나씩 검사하며 출력하므로(OK/BAD), 파일이 손상되어 TableFile 로 열 수 없는 경우에도
손상 위치 직전까지의 레코드와 손상 지점을 보여준다.
"""

from __future__ import annotations

import argparse
import sys
import warnings
from collections.abc import Sequence
from typing import Any, TextIO

from ..catalog import Catalog
from ..errors import DatabaseError
from ..identifiers import fold_identifier
from ..storage import FLAG_DELETED, RecordCheck, TableFile, inspect_records, read_header

__all__ = ["dump", "format_value", "main", "resolve_table_arg"]


def format_value(v: Any) -> str:
    """값 하나를 출력용 문자열로 바꾼다. NULL 은 ``NULL``, 문자열은 repr 로 따옴표 표시."""
    if v is None:
        return "NULL"
    if isinstance(v, str):
        return repr(v)
    return str(v)


def _flags_str(flags: int | None) -> str:
    if flags is None:
        return "?"
    names = ["deleted"] if flags & FLAG_DELETED else []
    return f"0x{flags:02x}" + (f" ({','.join(names)})" if names else "")


def _crc_str(c: RecordCheck) -> str:
    """CRC 검증 결과: 둘 다 맞으면 OK, 아니면 어느 쪽이 깨졌는지 BAD(...)."""
    if c.header_ok and c.body_ok:
        return "OK"
    if c.length is None:
        return "-"
    if not c.header_ok:
        return "BAD(header)"
    if c.body_ok is False:
        return "BAD(body)"
    return "OK"  # CRC 는 맞지만 다른 구조 문제 (error 에 표시)


def resolve_table_arg(arg: str) -> str:
    """명령줄 테이블 인자를 카탈로그 이름으로 바꾼다 (SQL 의 식별자 규칙과 동일).

    ``"Users"`` 처럼 큰따옴표로 감싸면 따옴표를 벗기고 ``""`` 를 ``"`` 로 바꿔 그대로 쓰고,
    아니면 :func:`~minidb.identifiers.fold_identifier` 로 ASCII 대문자만 소문자로 접는다.
    """
    if len(arg) >= 2 and arg[0] == '"' and arg[-1] == '"':
        return arg[1:-1].replace('""', '"')
    return fold_identifier(arg)


def dump(table: str, db_dir: str, *, limit: int | None = None, out: TextIO = sys.stdout) -> int:
    """``table`` 의 헤더와 레코드를 ``out`` 에 출력하고 검사한 레코드 위치 수를 반환한다.

    ``table`` 은 명령줄 인자 형태 그대로 받는다 (:func:`resolve_table_arg` 참고).
    """
    catalog = Catalog(db_dir)
    schema = catalog.get(resolve_table_arg(table))
    path = catalog.data_path(schema.name)
    if not path.exists():
        raise FileNotFoundError(f"table file not found: {path}")

    with open(path, "rb") as f:
        magic, version, column_count = read_header(f)
    file_size = path.stat().st_size

    # TableFile(readonly) 로 열어 next_id/논리적 끝을 얻는다. 손상이면 그 사실만 표시하고 계속.
    open_status = "ok"
    next_id: int | str = "?"
    logical_end: int | str = "?"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            with TableFile(path, schema, readonly=True) as tf:
                next_id, logical_end = tf.next_id, tf.size
        except DatabaseError as exc:
            open_status = f"FAILED: {exc}"
    if caught and open_status == "ok":
        open_status = f"warning: {caught[0].message}"

    checks = list(inspect_records(path, schema))
    good = sum(1 for c in checks if c.error is None)
    deleted = sum(1 for c in checks if c.error is None and c.flags is not None and c.flags & FLAG_DELETED)
    bad = len(checks) - good

    print(f"table          : {schema.name}  (oid {schema.oid})", file=out)
    print(f"file           : {path}", file=out)
    print(f"magic          : {magic!r}", file=out)
    print(f"format_version : {version}", file=out)
    cols = ", ".join(f"{c.name} {c.type}" for c in schema.columns)
    print(f"column_count   : {column_count}  ({cols})", file=out)
    print(f"file_size      : {file_size} bytes (logical end {logical_end})", file=out)
    print(f"records        : {good}  (live {good - deleted}, deleted {deleted}, bad {bad})", file=out)
    print(f"next_id        : {next_id}", file=out)
    print(f"open           : {open_status}", file=out)
    print("-" * 72, file=out)
    print(f"{'offset':>12}  {'crc':<11}  {'flags':<14}  row ({', '.join(schema.column_names)})", file=out)

    for n, c in enumerate(checks):
        if limit is not None and n >= limit:
            print(f"... ({len(checks) - limit} more)", file=out)
            break
        if c.row is not None:
            body = "(" + ", ".join(format_value(v) for v in c.row) + ")"
        else:
            body = f"<{c.error}>"
        print(f"{c.offset:>12}  {_crc_str(c):<11}  {_flags_str(c.flags):<14}  {body}", file=out)
    return len(checks)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 진입점. 성공 시 0, 데이터베이스 오류 시 1 을 반환한다."""
    parser = argparse.ArgumentParser(prog="python -m minidb.tools.dump", description=__doc__.splitlines()[0])
    parser.add_argument("table", help='테이블 이름 (SQL 처럼 비인용은 소문자로 접고, \'"Users"\' 처럼 인용하면 그대로)')
    parser.add_argument("--db", required=True, help="DB 디렉터리 (schema/, data/ 를 포함)")
    parser.add_argument("--limit", type=int, default=None, help="출력할 최대 레코드 수")
    args = parser.parse_args(argv)
    try:
        dump(args.table, args.db, limit=args.limit)
    except DatabaseError as exc:
        print(exc, file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"ERROR:  {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:  # `| head` 등
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
