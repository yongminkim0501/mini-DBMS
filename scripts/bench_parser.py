"""SQL 렉서/파서 벤치마크.

측정 항목:
  1. 10,000행 INSERT 한 문장: 렉싱 시간, 파싱 시간(토큰 재사용)을 따로
  2. 위 문장 100개(총 100만 행) 스크립트: split_statements 시간 + 전체 파싱 시간
     (참고용으로 iter_statements 로 한 문장씩 분리→파싱→AST 폐기하는 스트리밍 처리 시간도 잰다)
  3. SELECT 단문 10만 개 파싱 시간 (Phase 3 요청당 파싱 비용의 기준값)

사용 예::

    uv run --python 3.14+gil python scripts/bench_parser.py --out results/parser_3.14.json
    uv run --python 3.14t    python scripts/bench_parser.py --out results/parser_3.14t.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import sys
import sysconfig
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minidb.sql.lexer import StatementSource, iter_statements, split_statements, tokenize  # noqa: E402
from minidb.sql.parser import parse_statement  # noqa: E402

_HANGUL = [chr(c) for c in range(0xAC00, 0xD7A4)]
_ASCII = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def gen_name(rng: random.Random) -> str:
    n = rng.randint(2, 12)
    ratio = rng.random()
    s = "".join(rng.choice(_HANGUL) if rng.random() < ratio else rng.choice(_ASCII) for _ in range(n))
    if rng.random() < 0.02:
        s = s[:1] + "'" + s[1:]  # 가끔 이스케이프가 필요한 따옴표
    return s


def gen_insert(rng: random.Random, rows: int) -> str:
    vals = []
    for _ in range(rows):
        name = "NULL" if rng.random() < 0.10 else "'" + gen_name(rng).replace("'", "''") + "'"
        age = "NULL" if rng.random() < 0.10 else str(rng.randint(0, 100))
        vals.append(f"({name}, {age})")
    return "INSERT INTO users (name, age) VALUES\n  " + ",\n  ".join(vals)


def gen_select(rng: random.Random) -> str:
    kind = rng.randrange(4)
    if kind == 0:
        return f"SELECT * FROM users WHERE id = {rng.randint(1, 1_000_000)}"
    if kind == 1:
        return f"SELECT id, name FROM users WHERE age >= {rng.randint(0, 100)} AND age < {rng.randint(0, 100)} LIMIT 10"
    if kind == 2:
        return f"SELECT name, age FROM users WHERE name = '{gen_name(rng).replace("'", "''")}'"
    return f"SELECT * FROM users WHERE {rng.randint(1, 1000)} = id"


def gil_enabled() -> bool | str:
    fn = getattr(sys, "_is_gil_enabled", None)
    return fn() if fn is not None else "N/A"


def timed(fn, repeat: int = 1):
    """fn 을 repeat 번 실행해 가장 빠른 시간과 마지막 결과를 반환한다."""
    best = float("inf")
    result = None
    for _ in range(repeat):
        t0 = time.perf_counter()
        result = fn()
        best = min(best, time.perf_counter() - t0)
    return best, result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rows-per-insert", type=int, default=10_000)
    ap.add_argument("--inserts", type=int, default=100)
    ap.add_argument("--selects", type=int, default=100_000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    env = {
        "python_version": sys.version.split()[0],
        "free_threaded_build": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
        "gil_enabled": gil_enabled(),
        "cpu_count": os.cpu_count(),
        "platform": platform.platform(),
    }
    print("== environment ==")
    for k, v in env.items():
        print(f"  {k:20}: {v}")

    rng = random.Random(args.seed)
    insert_sql = gen_insert(rng, args.rows_per_insert)

    # 1) 10,000행 INSERT 한 문장: 렉싱 / 파싱(이미 만든 토큰 사용) 분리 측정
    lex_s, toks = timed(lambda: tokenize(insert_sql), repeat=3)
    src = StatementSource(insert_sql, 0, None, tuple(toks[:-1]))
    parse_s, stmt = timed(lambda: parse_statement(src), repeat=3)
    assert len(stmt.rows) == args.rows_per_insert
    print(f"\n== single INSERT ({args.rows_per_insert:,} rows, {len(insert_sql):,} chars, {len(toks):,} tokens) ==")
    print(f"  lex                 : {lex_s * 1000:.1f} ms  ({len(toks) / lex_s:,.0f} tokens/s)")
    print(f"  parse (tokens given): {parse_s * 1000:.1f} ms  ({len(toks) / parse_s:,.0f} tokens/s)")

    # 2) 100개 INSERT = 100만 행 스크립트
    script = ";\n".join(gen_insert(rng, args.rows_per_insert) for _ in range(args.inserts)) + ";"
    total_rows = args.rows_per_insert * args.inserts
    split_s, sources = timed(lambda: split_statements(script))
    assert len(sources) == args.inserts and all(s.error is None for s in sources)
    t0 = time.perf_counter()
    nrows = 0
    for s in sources:
        nrows += len(parse_statement(s).rows)
    script_parse_s = time.perf_counter() - t0
    assert nrows == total_rows
    script_total = split_s + script_parse_s
    print(f"\n== script ({args.inserts} INSERTs, {total_rows:,} rows, {len(script) / 2**20:.1f} MiB) ==")
    print(f"  split_statements    : {split_s:.3f} s  (lexing included)")
    print(f"  parse all           : {script_parse_s:.3f} s")
    print(f"  total               : {script_total:.3f} s  ({total_rows / script_total:,.0f} rows/s)")
    del sources
    # 스트리밍: 문장마다 분리→파싱 후 바로 버린다 (살아 있는 객체가 적어 GC 부담이 작다)
    t0 = time.perf_counter()
    nrows = 0
    for s in iter_statements(script):
        nrows += len(parse_statement(s).rows)
    stream_s = time.perf_counter() - t0
    assert nrows == total_rows
    print(f"  streaming (iter)    : {stream_s:.3f} s  ({total_rows / stream_s:,.0f} rows/s)")
    del script

    # 3) SELECT 단문 10만 개 (문자열 → AST, 렉싱 포함)
    selects = [gen_select(rng) for _ in range(args.selects)]
    t0 = time.perf_counter()
    for q in selects:
        parse_statement(q)
    sel_s = time.perf_counter() - t0
    print(f"\n== {args.selects:,} SELECT statements ==")
    print(f"  parse (incl. lex)   : {sel_s:.3f} s  (avg {sel_s / args.selects * 1e6:.2f} µs/stmt)")

    result = {
        "benchmark": "parser",
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "environment": env,
        "params": {
            "rows_per_insert": args.rows_per_insert,
            "inserts": args.inserts,
            "selects": args.selects,
            "seed": args.seed,
        },
        "results": {
            "single_insert_chars": len(insert_sql),
            "single_insert_tokens": len(toks),
            "single_insert_lex_seconds": round(lex_s, 5),
            "single_insert_parse_seconds": round(parse_s, 5),
            "script_rows": total_rows,
            "script_split_seconds": round(split_s, 4),
            "script_parse_seconds": round(script_parse_s, 4),
            "script_total_seconds": round(script_total, 4),
            "script_rows_per_sec": round(total_rows / script_total, 1),
            "script_streaming_seconds": round(stream_s, 4),
            "select_count": args.selects,
            "select_total_seconds": round(sel_s, 4),
            "select_avg_us": round(sel_s / args.selects * 1e6, 3),
        },
    }
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"\nresult written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
