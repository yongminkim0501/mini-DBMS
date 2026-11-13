"""pgbench(PostgreSQL 공식 벤치마크 도구)의 사용자 정의 스크립트 모드로 minidb PG 프로토콜 서버에 부하를 건다.

조건 (Phase 3 의 scripts/bench_server.py 와 같다):
  서버  gil (3.14.6+gil) / ft (3.14.6t, PYTHON_GIL=0) / ft_gil1 (3.14.6t, PYTHON_GIL=1: 대조군)
  워커  1, 4, CPU 코어 수
  모드  -M simple (Q 메시지) / extended (매번 Parse+Bind+Execute) / prepared (Parse 는 한 번, 이후 Bind+Execute)
  부하  point (point.sql), insert (insert.sql), mixed (point 9 : insert 1, ``-f file@weight``)

실행 한 번: ``pgbench -n -h 127.0.0.1 -p <port> -U bench -d minidb -c 32 -j 4 -T 10 -M <mode> -f ...``
(-n: vacuum 생략. 사용자 정의 스크립트만 쓰면 pgbench 는 시작할 때 다른 쿼리를 보내지 않는다.)
설정마다 3초 워밍업 실행 1번 뒤 측정 3회. 서버 CPU 는 HTTP /stats 의 getrusage 차이 ÷ 경과 시간.

DB 는 Phase 3 실험의 기준 DB(bench_server_db/base: users 100만 건)를 (조건, 워커 수)마다 새로 복사해 쓴다.
결과: results/pgbench/<cond>_<workload>_<mode>_w<NN>_r<rep>.json, 집계는 --summary 로 results/pgbench/summary.json.

pgbench 가 없으면 아무것도 하지 않고 끝난다 (설치: macOS ``brew install postgresql@17``,
Ubuntu ``sudo apt-get install postgresql-contrib`` — README 참고).

사용 예::

    uv run --python 3.14.6+gil python scripts/pgbench/run_pgbench.py            # 전체 (약 50분)
    uv run --python 3.14.6+gil python scripts/pgbench/run_pgbench.py --quick    # 짧은 확인
    uv run --python 3.14.6+gil python scripts/pgbench/run_pgbench.py --summary  # 집계만
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import loadgen  # noqa: E402
from bench_server import CONDITIONS, free_port, prepare_base, stop_server, wait_ready  # noqa: E402

WORKLOADS: dict[str, list[str]] = {
    "point": ["-f", str(HERE / "point.sql")],
    "insert": ["-f", str(HERE / "insert.sql")],
    "mixed": ["-f", f"{HERE / 'point.sql'}@9", "-f", f"{HERE / 'insert.sql'}@1"],
}
MODES = ("simple", "extended", "prepared")


def start_server(cond: str, db_dir: Path, workers: int, http_port: int, pg_port: int, log_path: Path):
    python, extra_env = CONDITIONS[cond]
    env = dict(os.environ)
    env["UV_PYTHON_PREFERENCE"] = "only-managed"
    env.pop("PYTHON_GIL", None)
    env.update(extra_env)
    cmd = [
        "uv", "run", "--python", python, "python", "-m", "minidb", "serve",
        "--db", str(db_dir), "--http-port", str(http_port), "--pg-port", str(pg_port),
        "--workers", str(workers), "--queue-size", "1024", "--max-connections", "1024", "--no-access-log",
    ]  # fmt: skip
    log = open(log_path, "ab")  # noqa: SIM115 - 서버 프로세스가 끝날 때까지 열어 둔다
    return subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)


_PATTERNS = {
    "tps": re.compile(r"^tps = ([0-9.]+) \(without initial connection time\)", re.M),
    "latency_avg_ms": re.compile(r"^latency average = ([0-9.]+) ms", re.M),
    "latency_stddev_ms": re.compile(r"^latency stddev = ([0-9.]+) ms", re.M),
    "transactions": re.compile(r"^number of transactions actually processed: (\d+)", re.M),
    "failed": re.compile(r"^number of failed transactions: (\d+)", re.M),
}


def parse_pgbench(text: str) -> dict[str, float]:
    """pgbench 출력에서 전체(스크립트별이 아닌) tps, 평균 지연, 처리/실패 건수를 꺼낸다."""
    out: dict[str, float] = {}
    for key, pat in _PATTERNS.items():
        m = pat.search(text)
        if m:
            out[key] = float(m.group(1))
    if "tps" not in out:
        raise RuntimeError(f"could not parse pgbench output:\n{text}")
    return out


def run_pgbench(pgbench: str, port: int, workload: str, mode: str, duration: int, clients: int, threads: int) -> str:
    cmd = [
        pgbench, "-n", "-h", "127.0.0.1", "-p", str(port), "-U", "bench", "-d", "minidb",
        "-c", str(clients), "-j", str(threads), "-T", str(duration), "-M", mode,
        *WORKLOADS[workload],
    ]  # fmt: skip
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    env["LC_ALL"] = "C"
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=duration + 120)
    if r.returncode != 0:
        raise RuntimeError(f"pgbench failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
    return r.stdout


def summarize(out_dir: Path) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for f in sorted(out_dir.glob("*_r*.json")):
        d = json.loads(f.read_text())
        groups.setdefault(f"{d['condition']}|{d['workload']}|{d['mode']}|{d['workers']}", []).append(d)
    summary = {}
    for key, runs in sorted(groups.items()):
        summary[key] = {
            "tps": statistics.median(r["tps"] for r in runs),
            "latency_avg_ms": statistics.median(r["latency_avg_ms"] for r in runs),
            "server_cpu_cores": statistics.median(r["server_cpu_cores"] for r in runs),
            "failed": sum(r.get("failed", 0) for r in runs),
            "runs": len(runs),
        }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--conditions", default="gil,ft,ft_gil1")
    ap.add_argument("--workers", default=f"1,4,{os.cpu_count()}")
    ap.add_argument("--workloads", default="point,insert,mixed")
    ap.add_argument("--modes", default=",".join(MODES))
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--duration", type=int, default=10)
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--clients", type=int, default=32)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--base-db", default="bench_server_db/base")
    ap.add_argument("--work-db", default="bench_server_db/pgbench_work")
    ap.add_argument("--out", default="results/pgbench")
    ap.add_argument("--quick", action="store_true", help="짧은 확인: 1회, 워밍업 1초, 측정 3초")
    ap.add_argument("--summary", action="store_true", help="실행하지 않고 결과 집계만")
    args = ap.parse_args()
    out_dir = ROOT / args.out
    if args.summary:
        for k, v in summarize(out_dir).items():
            print(f"{k:32} tps {v['tps']:>10,.0f}  lat {v['latency_avg_ms']:.3f} ms  cores {v['server_cpu_cores']:.2f}")
        return 0
    pgbench = shutil.which("pgbench")
    if pgbench is None:
        print("pgbench not found; skipping (see README for how to install)")
        return 0
    if args.quick:
        args.reps, args.warmup, args.duration = 1, 1, 3

    out_dir.mkdir(parents=True, exist_ok=True)
    base = ROOT / args.base_db
    prepare_base(base, 1_000_000, 100_000)
    log_path = out_dir.parent / "pgbench_server.log"
    conditions = [c for c in args.conditions.split(",") if c]
    workers_list = sorted({int(x) for x in args.workers.split(",")})
    workloads = [w for w in args.workloads.split(",") if w]
    modes = [m for m in args.modes.split(",") if m]
    total = len(conditions) * len(workers_list) * len(workloads) * len(modes) * args.reps
    version = subprocess.run([pgbench, "--version"], capture_output=True, text=True).stdout.strip()
    n = 0
    for cond in conditions:
        for workers in workers_list:
            work = ROOT / args.work_db
            if work.exists():
                shutil.rmtree(work)
            shutil.copytree(base, work)
            http_port, pg_port = free_port(), free_port()
            url = f"http://127.0.0.1:{http_port}"
            proc = start_server(cond, work, workers, http_port, pg_port, log_path)
            try:
                info = wait_ready(url)
                loadgen.fetch_json(url + "/query", "SELECT * FROM users WHERE id = 1")  # 테이블을 미리 연다
                for workload in workloads:
                    for mode in modes:
                        run_pgbench(pgbench, pg_port, workload, mode, args.warmup, args.clients, args.threads)
                        for rep in range(1, args.reps + 1):
                            n += 1
                            print(f"[{n}/{total}] {cond} w={workers} {workload} {mode} rep={rep} ...", end=" ", flush=True)
                            cpu0 = loadgen._cpu(url)  # noqa: SLF001
                            t0 = time.monotonic()
                            text = run_pgbench(
                                pgbench, pg_port, workload, mode, args.duration, args.clients, args.threads
                            )
                            cpu1 = loadgen._cpu(url)  # noqa: SLF001
                            elapsed = time.monotonic() - t0
                            r: dict[str, Any] = parse_pgbench(text)
                            r.update(
                                condition=cond, workers=workers, workload=workload, mode=mode, rep=rep,
                                clients=args.clients, threads=args.threads, duration_s=args.duration,
                                server_cpu_cores=(cpu1[0] - cpu0[0]) / elapsed, server_runtime=info["runtime"],
                                pgbench_version=version, raw=text,
                            )  # fmt: skip
                            name = f"{cond}_{workload}_{mode}_w{workers:02d}_r{rep}.json"
                            (out_dir / name).write_text(json.dumps(r, indent=2) + "\n", encoding="utf-8")
                            print(
                                f"tps {r['tps']:,.0f}  lat {r['latency_avg_ms']:.3f} ms  "
                                f"cores {r['server_cpu_cores']:.2f}  failed {int(r.get('failed', 0))}",
                                flush=True,
                            )
            finally:
                stop_server(proc)
    summarize(out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
