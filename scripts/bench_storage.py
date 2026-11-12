"""저장 계층 벤치마크: 대량 적재 / 전체 scan / 랜덤 read_at / 재오픈 시간을 측정한다.

사용 예::

    uv run --python 3.14  python scripts/bench_storage.py --rows 1000000 --db ./bench_db --seed 42 --out results/storage_3.14.json
    uv run --python 3.14t python scripts/bench_storage.py --rows 1000000 --db ./bench_db --seed 42 --out results/storage_3.14t.json

같은 시드면 두 인터프리터가 완전히 같은 데이터를 쓰므로 빌드 간 비교가 가능하다.
측정은 단일 스레드이며, 데이터 생성 시간은 적재 시간에서 제외한다.
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
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

# `python scripts/bench_storage.py` 로 실행해도 minidb 를 import 할 수 있게 한다.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minidb.catalog import Catalog  # noqa: E402
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

_HANGUL = [chr(c) for c in range(0xAC00, 0xD7A4)]  # 가 ~ 힣
_ASCII = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def gen_rows(rng: random.Random, n: int) -> list[tuple[str | None, int | None]]:
    """users 행 n개를 생성한다. name: 한글/영문 혼합 2~12자(10% NULL), age: 0~100(10% NULL)."""
    rows: list[tuple[str | None, int | None]] = []
    for _ in range(n):
        if rng.random() < 0.10:
            name = None
        else:
            length = rng.randint(2, 12)
            hangul_ratio = rng.random()
            name = "".join(
                rng.choice(_HANGUL) if rng.random() < hangul_ratio else rng.choice(_ASCII)
                for _ in range(length)
            )
        age = None if rng.random() < 0.10 else rng.randint(0, 100)
        rows.append((name, age))
    return rows


def batches(rng: random.Random, total: int, size: int) -> Iterator[list[tuple[str | None, int | None]]]:
    done = 0
    while done < total:
        n = min(size, total - done)
        yield gen_rows(rng, n)
        done += n


def gil_enabled() -> bool | str:
    """``sys._is_gil_enabled()`` 결과, 속성이 없으면 ``"N/A"``."""
    fn = getattr(sys, "_is_gil_enabled", None)
    return fn() if fn is not None else "N/A"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rows", type=int, default=1_000_000)
    ap.add_argument("--batch", type=int, default=10_000)
    ap.add_argument("--db", default="./bench_db")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--reads", type=int, default=10_000, help="랜덤 read_at 횟수")
    ap.add_argument("--out", default=None, help="결과 JSON 경로")
    args = ap.parse_args()

    env = {
        "python_version": sys.version.split()[0],
        "python_full_version": sys.version,
        "implementation": platform.python_implementation(),
        "free_threaded_build": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
        "gil_enabled": gil_enabled(),
        "cpu_count": os.cpu_count(),
        "platform": platform.platform(),
        "machine": platform.machine(),
    }
    print("== environment ==")
    for k in ("python_version", "free_threaded_build", "gil_enabled", "cpu_count", "platform"):
        print(f"  {k:20}: {env[k]}")

    db = Path(args.db)
    (db / "schema").mkdir(parents=True, exist_ok=True)
    # 스키마 파일 이름은 의미가 없지만 관례대로 <oid>.json, 데이터 파일은 <db>/data/<oid>.tbl
    (db / "schema" / f"{USERS_SCHEMA['oid']}.json").write_text(json.dumps(USERS_SCHEMA, indent=2), encoding="utf-8")
    catalog = Catalog(db)
    schema = catalog.get("users")
    data_path = catalog.data_path("users")
    data_path.unlink(missing_ok=True)  # 매 실행을 빈 파일에서 시작

    rng = random.Random(args.seed)
    print(f"\n== load: {args.rows:,} rows, batch {args.batch:,}, seed {args.seed} ==")
    gen_time = 0.0
    load_time = 0.0
    offsets: list[int] = []
    with TableFile(data_path, schema) as tf:
        it = batches(rng, args.rows, args.batch)
        while True:
            t0 = time.perf_counter()
            batch = next(it, None)
            t1 = time.perf_counter()
            if batch is None:
                break
            gen_time += t1 - t0
            res = tf.append_many(batch)
            load_time += time.perf_counter() - t1
            offsets.extend(off for _, off in res)
        t0 = time.perf_counter()
        tf.flush()
        flush_time = time.perf_counter() - t0
        t0 = time.perf_counter()
        tf.flush(durable=True)
        fsync_time = time.perf_counter() - t0
        load_time += flush_time
        next_id = tf.next_id
    file_size = data_path.stat().st_size
    rows_per_sec = args.rows / load_time if load_time else float("inf")
    print(f"  data generation     : {gen_time:.3f} s (excluded)")
    print(f"  load (append_many)  : {load_time:.3f} s  ({rows_per_sec:,.0f} rows/s)")
    print(f"  fsync               : {fsync_time * 1000:.1f} ms")
    print(f"  file size           : {file_size:,} bytes ({file_size / 2**20:.1f} MiB, {file_size / max(args.rows, 1):.1f} B/row)")

    # 재오픈: 전체 레코드를 훑어 next_id 를 복원하는 비용
    t0 = time.perf_counter()
    tf = TableFile(data_path, schema)
    reopen_time = time.perf_counter() - t0
    assert tf.next_id == next_id, (tf.next_id, next_id)
    print(f"  reopen (recovery)   : {reopen_time:.3f} s")

    with tf:
        t0 = time.perf_counter()
        count = 0
        for _ in tf.scan():
            count += 1
        scan_time = time.perf_counter() - t0
        assert count == args.rows, (count, args.rows)
        print(f"\n== scan ==\n  full scan           : {scan_time:.3f} s  ({count / scan_time:,.0f} rows/s)")

        read_rng = random.Random(args.seed + 1)
        n_reads = min(args.reads, len(offsets))
        sample = read_rng.sample(range(len(offsets)), n_reads)
        targets = [offsets[i] for i in sample]
        t0 = time.perf_counter()
        for off in targets:
            tf.read_at(off)
        read_total = time.perf_counter() - t0
        for i in sample[:100]:  # 정합성 확인: offset i 의 id 는 i+1
            row = tf.read_at(offsets[i])
            assert row is not None and row[0] == i + 1
        read_avg_us = read_total / n_reads * 1e6 if n_reads else 0.0
        print(f"\n== random read_at ==\n  {n_reads:,} reads       : {read_total:.3f} s  (avg {read_avg_us:.2f} µs/read)")

    result = {
        "benchmark": "storage",
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "environment": env,
        "params": {"rows": args.rows, "batch": args.batch, "seed": args.seed, "reads": n_reads},
        "results": {
            "load_seconds": round(load_time, 4),
            "load_rows_per_sec": round(rows_per_sec, 1),
            "data_generation_seconds": round(gen_time, 4),
            "fsync_seconds": round(fsync_time, 4),
            "file_size_bytes": file_size,
            "bytes_per_row": round(file_size / max(args.rows, 1), 2),
            "reopen_seconds": round(reopen_time, 4),
            "scan_seconds": round(scan_time, 4),
            "scan_rows_per_sec": round(count / scan_time, 1),
            "random_read_total_seconds": round(read_total, 4),
            "random_read_avg_us": round(read_avg_us, 3),
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
