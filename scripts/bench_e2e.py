"""종단(end-to-end) 벤치마크: SQL 스크립트 → 렉싱/파싱 → 바인딩 → 저장, 그리고 인덱스 없는 SELECT 기준값.

측정 항목:
  1. 100만 행 INSERT 스크립트를 ``Database.execute`` 로 실행한 총 시간
  2. 같은 스크립트를 단계별로 분해 측정 (새 DB 에서 한 번 더): 렉싱(문장 분리), 파싱, 바인딩, 실행(저장)
  3. ``WHERE id = N`` SELECT 100회 평균 (Phase 2 인덱스 비교의 "인덱스 없음" 기준값)
  4. ``WHERE name = '...'`` (다른 필드) SELECT 100회 평균
  5. 참고: ``LIMIT 10`` SELECT (이터레이터 모델의 조기 종료 확인)

사용 예::

    uv run --python 3.14.6+gil python scripts/bench_e2e.py --out results/e2e_3.14.6.json
    uv run --python 3.14.6t    python scripts/bench_e2e.py --out results/e2e_3.14.6t.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import shutil
import statistics
import sys
import sysconfig
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from gen_insert_sql import write_script  # noqa: E402

from minidb.binder import bind  # noqa: E402
from minidb.engine import Database  # noqa: E402
from minidb.errors import DatabaseError  # noqa: E402
from minidb.sql.parser import parse_statement  # noqa: E402
from minidb.sql.stream import iter_script_statements  # noqa: E402

USERS_SCHEMA = {
    "oid": 16384,
    "table": "users",
    "columns": [
        {"name": "id", "type": "BIGINT", "auto_increment": True},
        {"name": "name", "type": "TEXT", "nullable": True},
        {"name": "age", "type": "BIGINT", "nullable": True},
    ],
}


def fresh_db(db_dir: Path) -> Path:
    """빈 users 테이블만 있는 DB 디렉터리를 새로 만든다."""
    if db_dir.exists():
        shutil.rmtree(db_dir)
    (db_dir / "schema").mkdir(parents=True)
    (db_dir / "schema" / "16384.json").write_text(json.dumps(USERS_SCHEMA, indent=2), encoding="utf-8")
    return db_dir


def gil_enabled() -> bool | str:
    fn = getattr(sys, "_is_gil_enabled", None)
    return fn() if fn is not None else "N/A"


def load_total(db_dir: Path, sql_path: Path) -> tuple[float, int]:
    """Database.execute 로 스크립트 전체를 실행한 시간과 삽입 행 수."""
    sql = sql_path.read_text(encoding="utf-8")
    t0 = time.perf_counter()
    inserted = 0
    with Database(db_dir) as db:
        for r in db.execute(sql):
            if isinstance(r, DatabaseError):
                raise r
            inserted += int(r.command_tag.split()[-1])
        db.flush()
    return time.perf_counter() - t0, inserted


def load_breakdown(db_dir: Path, sql_path: Path) -> dict[str, float]:
    """같은 파이프라인을 단계별로 나눠 잰다 (CLI 와 같은 스트리밍 입력)."""
    lex = parse = bnd = store = 0.0
    clock = time.perf_counter
    with Database(db_dir) as db, open(sql_path, encoding="utf-8", newline="") as f:
        it = iter_script_statements(f)
        t_all = clock()
        while True:
            t0 = clock()
            st = next(it, None)  # 파일 읽기 + 렉싱 + 문장 분리
            t1 = clock()
            if st is None:
                lex += t1 - t0
                break
            stmt = parse_statement(st.source)
            t2 = clock()
            bound = bind(stmt, db.catalog)
            t3 = clock()
            db.execute_bound(bound).command_tag  # 계획 + append_many (인코딩/검증/쓰기)
            t4 = clock()
            lex += t1 - t0
            parse += t2 - t1
            bnd += t3 - t2
            store += t4 - t3
        total = clock() - t_all
    return {"lex_split_seconds": lex, "parse_seconds": parse, "bind_seconds": bnd, "execute_storage_seconds": store, "total_seconds": total}


def time_queries(db: Database, queries: list[str]) -> dict[str, float]:
    """쿼리마다 결과를 모두 읽는 데 걸린 시간 (ms) 통계."""
    times = []
    rows = 0
    for q in queries:
        t0 = time.perf_counter()
        res = db.execute_one(q)
        rows += len(res.fetchall())
        times.append((time.perf_counter() - t0) * 1000)
    return {
        "runs": len(times),
        "avg_ms": statistics.fmean(times),
        "median_ms": statistics.median(times),
        "min_ms": min(times),
        "max_ms": max(times),
        "avg_rows": rows / len(times),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rows", type=int, default=1_000_000)
    ap.add_argument("--batch", type=int, default=5_000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--sql", default="data/users_1m.sql", help="INSERT 스크립트 (없으면 생성)")
    ap.add_argument("--db", default="bench_e2e_db")
    ap.add_argument("--select-runs", type=int, default=100)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    env = {
        "python_version": platform.python_version(),
        "sys_version": sys.version,
        "free_threaded_build": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
        "gil_enabled": gil_enabled(),
        "cpu_count": os.cpu_count(),
        "platform": platform.platform(),
    }
    print("== environment ==")
    for k, v in env.items():
        print(f"  {k:20}: {v}")

    sql_path = Path(args.sql)
    if not sql_path.exists():
        print(f"\ngenerating {sql_path} ...")
        write_script(sql_path, args.rows, args.batch, args.seed)
    print(f"\nscript: {sql_path} ({sql_path.stat().st_size / 2**20:.1f} MiB)")
    db_dir = Path(args.db)

    # 1) Database.execute 로 전체 적재
    total_s, inserted = load_total(fresh_db(db_dir), sql_path)
    print(f"\n== load via Database.execute ==\n  total               : {total_s:.3f} s  ({inserted / total_s:,.0f} rows/s, {inserted:,} rows)")

    # 2) 단계별 분해 (새 DB 에서 다시)
    br = load_breakdown(fresh_db(db_dir), sql_path)
    print("\n== load breakdown (streamed, like the CLI) ==")
    for k in ("lex_split_seconds", "parse_seconds", "bind_seconds", "execute_storage_seconds", "total_seconds"):
        share = br[k] / br["total_seconds"] * 100
        print(f"  {k:24}: {br[k]:7.3f} s  ({share:5.1f}%)")

    # 3) 4) 5) SELECT 기준값 (인덱스 없음 → 모두 전체 스캔)
    rng = random.Random(args.seed + 7)
    with Database(db_dir) as db:
        names = [r[0] for r in db.execute_one("SELECT name FROM users WHERE age = 42 LIMIT 1000") if r[0] is not None]
        id_queries = [f"SELECT * FROM users WHERE id = {rng.randint(1, inserted)}" for _ in range(args.select_runs)]
        name_queries = [
            "SELECT * FROM users WHERE name = '" + rng.choice(names).replace("'", "''") + "'" for _ in range(args.select_runs)
        ]
        limit_queries = ["SELECT * FROM users LIMIT 10"] * args.select_runs
        print(f"\n== SELECT (no index, {args.select_runs} runs each) ==")
        sel = {}
        for label, qs in (("id_eq", id_queries), ("name_eq", name_queries), ("limit_10", limit_queries)):
            sel[label] = time_queries(db, qs)
            s = sel[label]
            print(f"  {label:10}: avg {s['avg_ms']:9.3f} ms  median {s['median_ms']:9.3f}  min {s['min_ms']:9.3f}  max {s['max_ms']:9.3f}  rows/query {s['avg_rows']:.2f}")

    result = {
        "benchmark": "e2e",
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "environment": env,
        "params": {"rows": inserted, "batch": args.batch, "seed": args.seed, "select_runs": args.select_runs},
        "results": {
            "load_execute_total_seconds": round(total_s, 4),
            "load_rows_per_sec": round(inserted / total_s, 1),
            "breakdown": {k: round(v, 4) for k, v in br.items()},
            "select": {k: {kk: round(vv, 4) for kk, vv in v.items()} for k, v in sel.items()},
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
