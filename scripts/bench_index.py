"""Phase 2 인덱스 벤치마크: B+ 트리 구성/검색, SQL 종단 점·범위 검색(인덱스 켬/끔), order 비교, 분할 정책 비교.

100만 건이 적재된 DB(``--db``)를 대상으로 한다. 없으면 gen_insert_sql.py 의 스크립트(``--sql``, 없으면 생성)로 적재한다.

측정 항목 (결과는 JSON, docs/phase2-report.md 에 정리):
  1. 인덱스 구성: 벌크 로딩(열기 포함/제외) vs 증분 삽입 100만 건. tracemalloc 메모리, 높이, 노드 수, 채움률.
  2. 점 검색 (무작위 id 10,000개): (a) BPlusTree.search (b) dict 조회 (c) SQL WHERE id = N 인덱스 켬
     (d) 같은 쿼리 인덱스 끔 (e) WHERE name = '...'   — (d)(e) 는 전체 스캔이라 횟수를 줄인다.
  3. 범위 검색: 결과 10 / 1,000 / 100,000건, 인덱스 켬/끔. (dict 는 키 순서가 없어 범위 검색 불가 → 전체 순회 필요)
  4. order 비교: 16/32/64/128/256 의 벌크 로딩 시간, 점 검색 시간, 높이, 메모리.
  5. 순차 삽입 분할 정책: 맨 오른쪽 분할 최적화 켬/끔의 채움률, 메모리, 삽입 시간.

사용 예::

    uv run --python 3.14.6+gil python scripts/bench_index.py --out results/index_3.14.6.json
    uv run --python 3.14.6t    python scripts/bench_index.py --out results/index_3.14.6t.json
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import random
import shutil
import statistics
import sys
import sysconfig
import time
import tracemalloc
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from gen_insert_sql import write_script  # noqa: E402

from minidb.engine import Database  # noqa: E402
from minidb.errors import DatabaseError  # noqa: E402
from minidb.index import DEFAULT_ORDER, BPlusTree  # noqa: E402
from minidb.session import Session  # noqa: E402
from minidb.storage import TableFile  # noqa: E402

USERS_SCHEMA = {
    "oid": 16384,
    "table": "users",
    "columns": [
        {"name": "id", "type": "BIGINT", "auto_increment": True},
        {"name": "name", "type": "TEXT", "nullable": True},
        {"name": "age", "type": "BIGINT", "nullable": True},
    ],
}
ORDERS = (16, 32, 64, 128, 256, 512)
clock = time.perf_counter


def gil_enabled() -> bool | str:
    fn = getattr(sys, "_is_gil_enabled", None)
    return fn() if fn is not None else "N/A"


def stats_ms(times: list[float]) -> dict[str, float]:
    ms = [t * 1000 for t in times]
    return {
        "runs": len(ms),
        "avg_ms": statistics.fmean(ms),
        "median_ms": statistics.median(ms),
        "min_ms": min(ms),
        "max_ms": max(ms),
    }


def traced(fn: Callable[[], Any]) -> tuple[Any, int]:
    """fn() 의 결과와, 그 결과가 붙잡고 있는 메모리(tracemalloc 기준 순증가 바이트)."""
    gc.collect()
    tracemalloc.start()
    base = tracemalloc.get_traced_memory()[0]
    obj = fn()
    gc.collect()
    used = tracemalloc.get_traced_memory()[0] - base
    tracemalloc.stop()
    return obj, used


def ensure_db(db_dir: Path, sql_path: Path, rows: int, batch: int, seed: int) -> dict[str, float] | None:
    """100만 건 DB 가 없으면 만들고 적재 시간을 반환한다 (있으면 None)."""
    data = db_dir / "data" / "16384.tbl"
    if data.exists():
        return None
    if db_dir.exists():
        shutil.rmtree(db_dir)
    (db_dir / "schema").mkdir(parents=True)
    (db_dir / "schema" / "16384.json").write_text(json.dumps(USERS_SCHEMA, indent=2), encoding="utf-8")
    if not sql_path.exists():
        print(f"generating {sql_path} ...")
        write_script(sql_path, rows, batch, seed)
    print(f"loading {sql_path} into {db_dir} (INSERT 마다 인덱스 증분 갱신 포함) ...")
    sql = sql_path.read_text(encoding="utf-8")
    t0 = clock()
    n = 0
    with Database(db_dir) as db:
        for r in db.execute(sql):
            if isinstance(r, DatabaseError):
                raise r
            n += int(r.command_tag.split()[-1])
        db.flush(durable=True)
    total = clock() - t0
    return {"load_seconds": total, "rows": n, "rows_per_sec": n / total}


def collect_entries(path: Path, schema: Any) -> list[tuple[int, int]]:
    entries: list[tuple[int, int]] = []
    TableFile(path, schema, readonly=True, on_entry=lambda k, v: entries.append((k, v))).close()
    return entries


def bulk_from(entries: list[tuple[int, int]], order: int = DEFAULT_ORDER) -> BPlusTree:
    b = BPlusTree.bulk_builder(order)
    add = b.add
    for k, v in entries:
        add(k, v)
    return b.finish()


def incremental_from(entries: list[tuple[int, int]], order: int = DEFAULT_ORDER, rightmost: bool = True) -> BPlusTree:
    t = BPlusTree(order, rightmost_split=rightmost)
    ins = t.insert
    for k, v in entries:
        ins(k, v)
    return t


def best_of(n: int, fn: Callable[[], Any]) -> tuple[float, Any]:
    best = float("inf")
    out = None
    for _ in range(n):
        gc.collect()
        t0 = clock()
        out = fn()
        best = min(best, clock() - t0)
    return best, out


def run_queries(db: Database, queries: list[str], session: Session) -> tuple[dict[str, float], float]:
    times = []
    rows = 0
    for q in queries:
        t0 = clock()
        rows += len(db.execute_one(q, session).fetchall())
        times.append(clock() - t0)
    s = stats_ms(times)
    s["avg_rows"] = rows / len(queries)
    return s, rows / len(queries)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rows", type=int, default=1_000_000)
    ap.add_argument("--batch", type=int, default=5_000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--sql", default="data/users_1m.sql")
    ap.add_argument("--db", default="bench_index_db")
    ap.add_argument("--lookups", type=int, default=10_000, help="점 검색 무작위 id 수")
    ap.add_argument("--scan-runs", type=int, default=20, help="전체 스캔 쿼리 반복 수")
    ap.add_argument("--repeat", type=int, default=3, help="구성 시간 측정 반복 (최솟값 사용)")
    ap.add_argument("--fresh", action="store_true", help="DB 를 지우고 다시 적재 (INSERT + 인덱스 증분 갱신 시간 측정)")
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

    db_dir = Path(args.db)
    if args.fresh and db_dir.exists():
        shutil.rmtree(db_dir)
    load = ensure_db(db_dir, Path(args.sql), args.rows, args.batch, args.seed)
    if load:
        print(f"  load: {load['load_seconds']:.3f} s ({load['rows_per_sec']:,.0f} rows/s)")
    results: dict[str, Any] = {"initial_load": load}
    rng = random.Random(args.seed)

    with Database(db_dir) as probe:
        schema = probe.catalog.get("users")
        path = probe.catalog.data_path("users")

    # ------------------------------------------------------------------ 1. 구성
    print("\n== 1. index build ==")
    open_only, _ = best_of(args.repeat, lambda: TableFile(path, schema).close())
    open_bulk, _ = best_of(
        args.repeat,
        lambda: (lambda b: (TableFile(path, schema, on_entry=b.add).close(), b.finish()))(BPlusTree.bulk_builder()),
    )
    entries = collect_entries(path, schema)
    n = len(entries)
    bulk_only, tree = best_of(args.repeat, lambda: bulk_from(entries))
    incr, tree_incr = best_of(args.repeat, lambda: incremental_from(entries))
    _, bulk_mem = traced(lambda: bulk_from(entries))
    _, incr_mem = traced(lambda: incremental_from(entries))
    _, dict_mem = traced(lambda: dict(entries))
    st = tree.stats()
    st_incr = tree_incr.stats()
    build = {
        "keys": n,
        "order": DEFAULT_ORDER,
        "open_verify_only_seconds": open_only,
        "open_with_bulk_index_seconds": open_bulk,
        "bulk_overhead_in_open_seconds": open_bulk - open_only,
        "bulk_only_seconds": bulk_only,
        "incremental_insert_seconds": incr,
        "bulk_memory_bytes": bulk_mem,
        "incremental_memory_bytes": incr_mem,
        "dict_memory_bytes": dict_mem,
        "bulk_stats": st,
        "incremental_stats": st_incr,
    }
    results["build"] = build
    print(f"  keys                      : {n:,}")
    print(f"  open (verify only)        : {open_only:.3f} s")
    print(f"  open + bulk index         : {open_bulk:.3f} s  (index overhead {open_bulk - open_only:.3f} s)")
    print(f"  bulk load only            : {bulk_only:.3f} s")
    print(f"  incremental insert (seq)  : {incr:.3f} s")
    print(f"  memory bulk / incr / dict : {bulk_mem / 2**20:.1f} / {incr_mem / 2**20:.1f} / {dict_mem / 2**20:.1f} MiB")
    print(f"  bulk tree                 : height {st['height']}, nodes {st['nodes']:,}, leaves {st['leaves']:,}, "
          f"leaf fill avg {st['leaf_fill_avg']:.3f}")  # fmt: skip
    print(f"  incremental tree          : height {st_incr['height']}, nodes {st_incr['nodes']:,}, "
          f"leaf fill avg {st_incr['leaf_fill_avg']:.3f}")  # fmt: skip
    del tree_incr

    # ------------------------------------------------------------------ 2. 점 검색
    print(f"\n== 2. point lookup ({args.lookups:,} random ids) ==")
    keys = [rng.randint(1, n) for _ in range(args.lookups)]
    d = dict(entries)
    search = tree.search

    def tree_lookups() -> None:
        for k in keys:
            search(k)

    def dict_lookups() -> None:
        get = d.get
        for k in keys:
            get(k)

    t_tree, _ = best_of(args.repeat, tree_lookups)
    t_dict, _ = best_of(args.repeat, dict_lookups)
    assert all(search(k) == d[k] for k in keys[:1000])
    del d

    on, off = Session(), Session(enable_indexscan=False)
    point: dict[str, Any] = {
        "lookups": args.lookups,
        "bptree_search_total_ms": t_tree * 1000,
        "bptree_search_us_per_lookup": t_tree / args.lookups * 1e6,
        "dict_get_total_ms": t_dict * 1000,
        "dict_get_us_per_lookup": t_dict / args.lookups * 1e6,
    }
    with Database(db_dir) as db:
        db.table_handle(db.catalog.get("users"))  # 열기(인덱스 구성)는 측정에서 제외
        names = [r[0] for r in db.execute_one("SELECT name FROM users WHERE age = 42 LIMIT 1000") if r[0] is not None]
        sql_on, _ = run_queries(db, [f"SELECT * FROM users WHERE id = {k}" for k in keys], on)
        off_keys = keys[: args.scan_runs]
        sql_off, _ = run_queries(db, [f"SELECT * FROM users WHERE id = {k}" for k in off_keys], off)
        name_q = ["SELECT * FROM users WHERE name = '" + rng.choice(names).replace("'", "''") + "'" for _ in range(args.scan_runs)]
        sql_name, _ = run_queries(db, name_q, on)
        point.update({"sql_id_eq_index_on": sql_on, "sql_id_eq_index_off": sql_off, "sql_name_eq": sql_name})
        print(f"  (a) BPlusTree.search      : {point['bptree_search_us_per_lookup']:.3f} us/lookup")
        print(f"  (b) dict.get              : {point['dict_get_us_per_lookup']:.3f} us/lookup")
        print(f"  (c) SQL id = N, index on  : avg {sql_on['avg_ms']:.4f} ms  median {sql_on['median_ms']:.4f}")
        print(f"  (d) SQL id = N, index off : avg {sql_off['avg_ms']:.1f} ms  ({sql_off['runs']} runs)")
        print(f"  (e) SQL name = '...'      : avg {sql_name['avg_ms']:.1f} ms  ({sql_name['runs']} runs)")
        print(f"  speedup (d)/(c)           : {sql_off['avg_ms'] / sql_on['avg_ms']:,.0f}x")
        results["point"] = point

        # -------------------------------------------------------------- 3. 범위
        print("\n== 3. range scan ==")
        rng_res: dict[str, Any] = {
            "note": "dict 는 키 순서를 유지하지 않아 범위 검색을 할 수 없다 (모든 키를 훑거나 정렬해야 함). "
            "B+ 트리는 하한 이상인 첫 키의 offset 을 O(log n) 에 찾고, 데이터 파일을 그 위치부터 순차로 읽는다 "
            "(클러스터드 범위 스캔, Heap Fetch: sequential).",
            "heap_fetch": "sequential"
        }
        # 범위 Index Scan 은 트리로 시작 offset 만 찾고 파일을 순차로 읽는다 (클러스터드 범위 스캔).
        # 전체의 50/90/100% 범위로 "범위가 크면 Seq Scan 보다 느려지는가"(이전 방식의 역전)를 확인한다.
        for size in (10, 1_000, 100_000, n // 2, n * 9 // 10, n):
            runs_on = 50 if size <= 1_000 else 10 if size <= 100_000 else 5
            runs_off = min(args.scan_runs, 10 if size <= 100_000 else 5)
            qs = []
            for _ in range(max(runs_on, runs_off)):
                lo = rng.randint(1, n - size + 1)
                qs.append(f"SELECT * FROM users WHERE id >= {lo} AND id < {lo + size}")
            s_on, rows_on = run_queries(db, qs[:runs_on], on)
            s_off, rows_off = run_queries(db, qs[:runs_off], off)
            assert rows_on == rows_off == size
            rng_res[str(size)] = {
                "fraction": size / n,
                "index_on": s_on,
                "index_off": s_off,
                "speedup": s_off["avg_ms"] / s_on["avg_ms"],
            }
            print(f"  {size:>9,} rows ({size / n:6.1%}): on avg {s_on['avg_ms']:9.3f} ms | off avg {s_off['avg_ms']:9.1f} ms | "
                  f"{s_off['avg_ms'] / s_on['avg_ms']:,.1f}x")  # fmt: skip
        results["range"] = rng_res

    # ------------------------------------------------------------------ 4. order 비교
    print("\n== 4. order comparison ==")
    orders: dict[str, Any] = {}
    order_keys = [rng.randint(1, n) for _ in range(100_000)]  # 점 검색 10만 회 (측정 잡음 줄이기)
    random_pairs = [(k, k) for k in rng.sample(range(1, 10_000_000), 300_000)]  # 무작위 순서 증분 삽입
    for order in ORDERS:
        t_bulk, tr = best_of(args.repeat, lambda o=order: bulk_from(entries, o))
        s = tr.search

        def lookups(s: Callable[[int], Any] = s) -> None:
            for k in order_keys:
                s(k)

        t_look, _ = best_of(args.repeat, lookups)
        t_ins, _ = best_of(1, lambda o=order: incremental_from(entries, o))
        t_rand, _ = best_of(2, lambda o=order: incremental_from(random_pairs, o))
        _, mem = traced(lambda o=order: bulk_from(entries, o))
        info = tr.stats()
        orders[str(order)] = {
            "bulk_seconds": t_bulk,
            "incremental_seconds": t_ins,
            "random_insert_300k_seconds": t_rand,
            "search_us_per_lookup": t_look / len(order_keys) * 1e6,
            "height": info["height"],
            "nodes": info["nodes"],
            "memory_bytes": mem,
        }
        print(f"  order {order:>3}: bulk {t_bulk:.3f} s | seq {t_ins:.3f} s | rand300k {t_rand:.3f} s | "
              f"search {t_look / len(order_keys) * 1e6:.3f} us | "
              f"height {info['height']} | nodes {info['nodes']:>6,} | {mem / 2**20:.1f} MiB")  # fmt: skip
        del tr
    results["orders"] = orders

    # ------------------------------------------------------------------ 5. 분할 정책
    print("\n== 5. sequential insert split policy ==")
    policy: dict[str, Any] = {}
    for label, rightmost in (("rightmost_on", True), ("rightmost_off", False)):
        t_ins, tr = best_of(args.repeat, lambda r=rightmost: incremental_from(entries, DEFAULT_ORDER, r))
        _, mem = traced(lambda r=rightmost: incremental_from(entries, DEFAULT_ORDER, r))
        info = tr.stats()
        policy[label] = {
            "insert_seconds": t_ins,
            "memory_bytes": mem,
            "leaf_fill_avg": info["leaf_fill_avg"],
            "internal_fill_avg": info["internal_fill_avg"],
            "height": info["height"],
            "nodes": info["nodes"],
        }
        print(f"  {label:13}: insert {t_ins:.3f} s | leaf fill {info['leaf_fill_avg']:.3f} | "
              f"nodes {info['nodes']:,} | height {info['height']} | {mem / 2**20:.1f} MiB")  # fmt: skip
        del tr
    results["split_policy"] = policy

    out = {
        "benchmark": "index",
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "environment": env,
        "params": {
            "rows": n,
            "lookups": args.lookups,
            "scan_runs": args.scan_runs,
            "seed": args.seed,
            "default_order": DEFAULT_ORDER,
            "repeat": args.repeat,
        },
        "results": results,
    }
    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"\nresult written to {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
