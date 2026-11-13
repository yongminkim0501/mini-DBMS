"""Phase 3 서버 성능 실험: 서버 조건 × 워커 수 × 워크로드 × 반복.

서버 조건 (서버 프로세스를 따로 띄운다):
  gil      uv run --python 3.14.6+gil            (GIL 빌드)
  ft       uv run --python 3.14.6t               (free-threaded, GIL 꺼짐)
  ft_gil1  uv run --python 3.14.6t, PYTHON_GIL=1 (같은 free-threaded 바이너리에서 GIL 만 켬: 대조군)

워커 수: 1, 2, 4, 8, CPU 코어 수. 워크로드: point / scan / insert / mixed (scripts/loadgen.py).
측정: 워밍업 3초 + 10초, 조건마다 3회. 결과는 실행 하나당 JSON 하나로 results/server/ 에 저장한다.

DB 준비: users 100만 건(--seed 42, Phase 2 와 같은 데이터) + people 10만 건. 한 번 만든 기준 DB 를
(조건, 워커 수)마다 새로 복사해 쓴다(INSERT 워크로드가 테이블을 키우므로 조건 사이에 같은 출발점을 맞춘다).

부하 생성기는 이 스크립트를 실행한 파이썬(보통 3.14.6 GIL)에서 multiprocessing 으로 돈다. 같은 머신에서 서버와 CPU 를
나눠 쓴다는 한계는 docs/phase3-report.md 에 적었다.

사용 예::

    uv run --python 3.14.6+gil python scripts/bench_server.py              # 전체 (약 50분)
    uv run --python 3.14.6+gil python scripts/bench_server.py --quick      # 짧은 확인용
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import loadgen  # noqa: E402
from gen_insert_sql import iter_batches  # noqa: E402

from minidb.engine import Database  # noqa: E402
from minidb.errors import DatabaseError  # noqa: E402

CONDITIONS: dict[str, tuple[str, dict[str, str]]] = {
    "gil": ("3.14.6+gil", {}),
    "ft": ("3.14.6t", {"PYTHON_GIL": "0"}),
    "ft_gil1": ("3.14.6t", {"PYTHON_GIL": "1"}),
}


def schema(oid: int, table: str) -> dict[str, Any]:
    return {
        "oid": oid,
        "table": table,
        "columns": [
            {"name": "id", "type": "BIGINT", "auto_increment": True},
            {"name": "name", "type": "TEXT", "nullable": True},
            {"name": "age", "type": "BIGINT", "nullable": True},
        ],
    }


def prepare_base(base: Path, users_rows: int, people_rows: int) -> None:
    marker = base / "READY"
    if marker.exists():
        return
    if base.exists():
        shutil.rmtree(base)
    (base / "schema").mkdir(parents=True)
    (base / "schema" / "16384.json").write_text(json.dumps(schema(16384, "users")), encoding="utf-8")
    (base / "schema" / "16385.json").write_text(json.dumps(schema(16385, "people")), encoding="utf-8")
    print(f"preparing base DB {base}: users {users_rows:,}, people {people_rows:,} ...", flush=True)
    t0 = time.perf_counter()
    with Database(base) as db:
        for table, rows, seed in (("users", users_rows, 42), ("people", people_rows, 7)):
            for stmt in iter_batches(rows, 5000, seed):
                sql = stmt.replace("INSERT INTO users ", f"INSERT INTO {table} ", 1)
                for r in db.execute(sql):
                    if isinstance(r, DatabaseError):
                        raise r
        db.flush(durable=True)
    marker.write_text("ok\n")
    print(f"  done in {time.perf_counter() - t0:.1f} s", flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_ready(url: str, timeout: float = 120.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/health", timeout=2) as r:
                if r.status == 200:
                    return loadgen.fetch_json(url + "/stats")
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"server at {url} did not become ready")


def start_server(cond: str, db_dir: Path, workers: int, port: int, log_path: Path) -> subprocess.Popen:
    python, extra_env = CONDITIONS[cond]
    env = dict(os.environ)
    env["UV_PYTHON_PREFERENCE"] = "only-managed"
    env.pop("PYTHON_GIL", None)
    env.update(extra_env)
    cmd = [
        "uv", "run", "--python", python, "python", "-m", "minidb", "serve",
        "--db", str(db_dir), "--port", str(port), "--workers", str(workers),
        "--queue-size", "1024", "--max-connections", "1024", "--no-access-log",
    ]  # fmt: skip
    log = open(log_path, "ab")
    return subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)


def stop_server(p: subprocess.Popen) -> None:
    p.terminate()  # SIGTERM → 정상 종료 (uv 가 자식에게 전달)
    try:
        p.wait(60)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--conditions", default="gil,ft,ft_gil1")
    ap.add_argument("--workers", default=f"1,2,4,8,{os.cpu_count()}")
    ap.add_argument("--workloads", default="point,scan,insert,mixed")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--warmup", type=float, default=3.0)
    ap.add_argument("--duration", type=float, default=10.0)
    ap.add_argument("--procs", type=int, default=4)
    ap.add_argument("--conns", type=int, default=8)
    ap.add_argument("--users", type=int, default=1_000_000)
    ap.add_argument("--people", type=int, default=100_000)
    ap.add_argument("--base-db", default="bench_server_db/base")
    ap.add_argument("--work-db", default="bench_server_db/work")
    ap.add_argument("--out", default="results/server")
    ap.add_argument("--quick", action="store_true", help="짧은 확인: 1회, 워밍업 1초, 측정 3초")
    args = ap.parse_args()
    if args.quick:
        args.reps, args.warmup, args.duration = 1, 1.0, 3.0

    conditions = [c for c in args.conditions.split(",") if c]
    workers_list = sorted({int(x) for x in args.workers.split(",")})
    workloads = [w for w in args.workloads.split(",") if w]
    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    base = ROOT / args.base_db
    prepare_base(base, args.users, args.people)
    log_path = out_dir.parent / "server_bench.log"

    total = len(conditions) * len(workers_list) * len(workloads) * args.reps
    n = 0
    for cond in conditions:
        for workers in workers_list:
            work = ROOT / args.work_db
            if work.exists():
                shutil.rmtree(work)
            shutil.copytree(base, work)
            port = free_port()
            url = f"http://127.0.0.1:{port}"
            proc = start_server(cond, work, workers, port, log_path)
            try:
                info = wait_ready(url)
                # 테이블을 미리 연다 (인덱스 구성은 측정에서 제외)
                loadgen.fetch_json(url + "/query", "SELECT * FROM users WHERE id = 1; SELECT * FROM people LIMIT 1")
                runtime = info["runtime"]
                for workload in workloads:
                    for rep in range(1, args.reps + 1):
                        n += 1
                        print(f"[{n}/{total}] {cond} workers={workers} {workload} rep={rep} ...", end=" ", flush=True)
                        r = loadgen.run(
                            url, workload, procs=args.procs, conns=args.conns, warmup=args.warmup,
                            duration=args.duration, max_id=args.users, seed=rep * 100 + workers,
                        )  # fmt: skip
                        r.update({"condition": cond, "workers": workers, "rep": rep, "server_runtime": runtime})
                        name = f"{cond}_{workload}_w{workers:02d}_r{rep}.json"
                        (out_dir / name).write_text(json.dumps(r, indent=2) + "\n", encoding="utf-8")
                        print(
                            f"{r['throughput_rps']:,.0f} req/s  p50 {r['latency_ms']['p50']:.2f} ms  "
                            f"p99 {r['latency_ms']['p99']:.2f} ms  cores {r['server_cpu_cores']:.2f}  "
                            f"503 {r['rejected_503']}  err {r['errors']}",
                            flush=True,
                        )
            finally:
                stop_server(proc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
