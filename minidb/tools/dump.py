"""테이블 데이터 파일을 사람이 읽을 수 있게 출력하는 도구.

사용법::

    python -m minidb.tools.dump <table> --db <db_dir> [--limit N]

파일을 수정하지 않도록 :class:`~minidb.storage.TableFile` 을 readonly 로 연다
(손상된 꼬리가 있어도 truncate 하지 않고 경고만 출력한다).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Any, TextIO

from ..catalog import Catalog
from ..errors import DatabaseError
from ..storage import FLAG_DELETED, TableFile, read_header

__all__ = ["dump", "format_value", "main"]


def format_value(v: Any) -> str:
    """값 하나를 출력용 문자열로 바꾼다. NULL 은 ``NULL``, 문자열은 repr 로 따옴표 표시."""
    if v is None:
        return "NULL"
    if isinstance(v, str):
        return repr(v)
    return str(v)


def _flags_str(flags: int) -> str:
    names = ["deleted"] if flags & FLAG_DELETED else []
    return f"0x{flags:02x}" + (f" ({','.join(names)})" if names else "")


def dump(table: str, db_dir: str, *, limit: int | None = None, out: TextIO = sys.stdout) -> int:
    """``table`` 의 헤더와 레코드를 ``out`` 에 출력하고 레코드 수를 반환한다."""
    catalog = Catalog(db_dir)
    schema = catalog.get(table)
    path = catalog.data_path(schema.name)

    with TableFile(path, schema, readonly=True) as tf:
        with open(path, "rb") as f:
            magic, version, column_count = read_header(f)
        # 레코드 수는 헤더에 없으므로 한 번 훑어서 센다 (출력과 같은 순회에서 세면
        # --limit 으로 출력을 줄여도 정확한 수를 헤더 요약에 보여줄 수 없다).
        total = deleted = 0
        for _, flags, _ in tf.records():
            total += 1
            deleted += bool(flags & FLAG_DELETED)

        print(f"table          : {schema.name}", file=out)
        print(f"file           : {path}", file=out)
        print(f"magic          : {magic!r}", file=out)
        print(f"format_version : {version}", file=out)
        print(f"column_count   : {column_count}  ({', '.join(f'{c.name} {c.type}' for c in schema.columns)})", file=out)
        print(f"file_size      : {tf.size} bytes", file=out)
        print(f"records        : {total}  (live {total - deleted}, deleted {deleted})", file=out)
        print(f"next_id        : {tf.next_id}", file=out)
        print("-" * 60, file=out)
        print(f"{'offset':>12}  {'flags':<14}  row ({', '.join(schema.column_names)})", file=out)

        for n, (offset, flags, row) in enumerate(tf.records()):
            if limit is not None and n >= limit:
                print(f"... ({total - limit} more)", file=out)
                break
            values = ", ".join(format_value(v) for v in row)
            print(f"{offset:>12}  {_flags_str(flags):<14}  ({values})", file=out)
    return total


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 진입점. 성공 시 0, 데이터베이스 오류 시 1 을 반환한다."""
    parser = argparse.ArgumentParser(prog="python -m minidb.tools.dump", description=__doc__.splitlines()[0])
    parser.add_argument("table", help="테이블 이름")
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
