"""닫힌 루프(closed-loop) HTTP 부하 생성기 (표준 라이브러리만).

클라이언트 프로세스 P 개(multiprocessing), 프로세스마다 keep-alive 연결 C 개. 각 연결은 응답을 받자마자 다음 요청을 보낸다.
프로세스 안에서는 스레드 대신 selectors 로 C 개 연결을 한 스레드가 돌린다(부하 생성기 자신의 CPU 비용을 줄이려고).

워크로드 (``--workload``):
  point   SELECT * FROM users WHERE id = <무작위 1..--max-id>
  scan    SELECT * FROM people WHERE name = '<무작위 이름>'      (people 10만 건, Seq Scan)
  insert  INSERT INTO users (name, age) VALUES ('lg', 1)
  mixed   point 90% + insert 10%

측정: ``--warmup`` 초 뒤 ``--duration`` 초 동안 완료된 요청만 센다. 처리량, 지연 p50/p95/p99, 상태 코드별 수.
서버 CPU: 측정 구간 앞뒤로 /stats 의 getrusage(user+sys)를 읽어 ``CPU 초 ÷ 경과 초`` = 실제로 쓴 코어 수.

사용 예::

    python scripts/loadgen.py --url http://127.0.0.1:8080 --workload point --procs 4 --conns 8 --max-id 1000000
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import platform
import random
import selectors
import socket
import sys
import time
import urllib.request
from typing import Any
from urllib.parse import urlsplit

WORKLOADS = ("point", "scan", "insert", "mixed")


def _request(host: str, port: int, sql: str) -> bytes:
    body = sql.encode("utf-8")
    head = (
        f"POST /query HTTP/1.1\r\nHost: {host}:{port}\r\nContent-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    )
    return head.encode("ascii") + body


def _make_sql(workload: str, rng: random.Random, max_id: int, names: list[str]) -> str:
    if workload == "mixed":
        workload = "insert" if rng.random() < 0.1 else "point"
    if workload == "point":
        return f"SELECT * FROM users WHERE id = {rng.randint(1, max_id)}"
    if workload == "scan":
        return "SELECT * FROM people WHERE name = '" + rng.choice(names).replace("'", "''") + "'"
    return "INSERT INTO users (name, age) VALUES ('lg', 1)"


class _Conn:
    __slots__ = ("sock", "out", "sent_at", "buf", "need", "code", "closing")

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.out = b""
        self.sent_at = 0.0
        self.buf = bytearray()
        self.need = -1  # 응답 전체 길이 (헤더를 다 받으면 결정)
        self.code = ""
        self.closing = False


def _client_proc(args: dict[str, Any], start_at: float, out_q: mp.Queue) -> None:
    host, port = args["host"], args["port"]
    rng = random.Random(args["seed"])
    workload, max_id, names = args["workload"], args["max_id"], args["names"]
    measure_from = start_at + args["warmup"]
    stop_at = measure_from + args["duration"]
    sel = selectors.DefaultSelector()
    conns = []
    for _ in range(args["conns"]):
        s = socket.create_connection((host, port))
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.setblocking(False)
        c = _Conn(s)
        conns.append(c)
    while time.time() < start_at:  # 모든 프로세스가 같은 시각에 시작
        time.sleep(0.001)
    latencies: list[float] = []
    status: dict[str, int] = {}
    errors = 0

    def send(c: _Conn) -> None:
        c.out = _request(host, port, _make_sql(workload, rng, max_id, names))
        c.sent_at = time.perf_counter()
        c.buf.clear()
        c.need = -1
        try:
            n = c.sock.send(c.out)
            c.out = c.out[n:]
        except BlockingIOError:
            pass
        sel.register(c.sock, selectors.EVENT_READ | (selectors.EVENT_WRITE if c.out else 0), c)

    for c in conns:
        send(c)
    open_conns = len(conns)
    while open_conns:
        now = time.time()
        if now >= stop_at:
            break
        for key, ev in sel.select(timeout=0.2):
            c = key.data
            if ev & selectors.EVENT_WRITE and c.out:
                try:
                    n = c.sock.send(c.out)
                    c.out = c.out[n:]
                except BlockingIOError:
                    pass
                if not c.out:
                    sel.modify(c.sock, selectors.EVENT_READ, c)
            if not ev & selectors.EVENT_READ:
                continue
            try:
                data = c.sock.recv(65536)
            except BlockingIOError:
                continue
            except OSError:
                data = b""
            if not data:
                sel.unregister(c.sock)
                c.sock.close()
                open_conns -= 1
                errors += 1
                continue
            c.buf += data
            if c.need < 0:
                end = c.buf.find(b"\r\n\r\n")
                if end < 0:
                    continue
                head = bytes(c.buf[:end]).decode("latin-1").split("\r\n")
                length = 0
                closing = False
                for line in head[1:]:
                    k, _, v = line.partition(":")
                    k = k.strip().lower()
                    if k == "content-length":
                        length = int(v)
                    elif k == "connection" and v.strip().lower() == "close":
                        closing = True
                c.need = end + 4 + length
                c.code = head[0].split(" ")[1]
                c.closing = closing
            if len(c.buf) < c.need:
                continue
            code, closing = c.code, c.closing
            done = time.perf_counter()
            if time.time() >= measure_from:
                latencies.append(done - c.sent_at)
                status[code] = status.get(code, 0) + 1
            sel.unregister(c.sock)
            if closing:  # 서버가 닫았다 (503 등): 새 연결로 계속
                c.sock.close()
                try:
                    s = socket.create_connection((host, port))
                    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    s.setblocking(False)
                    c.sock = s
                except OSError:
                    open_conns -= 1
                    errors += 1
                    continue
            c.out = b""
            send(c)
    for c in conns:
        try:
            c.sock.close()
        except OSError:
            pass
    out_q.put({"latencies": latencies, "status": status, "errors": errors})


def fetch_json(url: str, sql: str | None = None) -> Any:
    if sql is None:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.load(r)
    req = urllib.request.Request(url, data=sql.encode(), headers={"Content-Type": "text/plain"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _cpu(url: str) -> tuple[float, float]:
    st = fetch_json(url + "/stats")
    p = st["process"]
    return p["cpu_user_seconds"] + p["cpu_system_seconds"], time.time()


def percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    k = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


def run(
    url: str,
    workload: str,
    *,
    procs: int = 4,
    conns: int = 8,
    warmup: float = 3.0,
    duration: float = 10.0,
    max_id: int = 1_000_000,
    seed: int = 1,
) -> dict[str, Any]:
    parts = urlsplit(url)
    host, port = parts.hostname or "127.0.0.1", parts.port or 80
    names: list[str] = []
    if workload == "scan":
        res = fetch_json(url + "/query", "SELECT name FROM people WHERE age > 50 LIMIT 2000")
        names = [r[0] for r in res["results"][0]["rows"] if r[0] is not None]
    ctx = mp.get_context("spawn")
    q: mp.Queue = ctx.Queue()
    start_at = time.time() + 1.0 + 0.2 * procs
    ps = []
    for i in range(procs):
        a = {"host": host, "port": port, "workload": workload, "conns": conns, "warmup": warmup,
             "duration": duration, "max_id": max_id, "names": names, "seed": seed * 1000 + i}  # fmt: skip
        p = ctx.Process(target=_client_proc, args=(a, start_at, q), daemon=True)
        p.start()
        ps.append(p)
    # 측정 구간 앞뒤로 서버 CPU 사용량을 읽는다
    while time.time() < start_at + warmup:
        time.sleep(0.005)
    cpu0, t0 = _cpu(url)
    while time.time() < start_at + warmup + duration:
        time.sleep(0.005)
    cpu1, t1 = _cpu(url)
    parts_out = [q.get(timeout=duration + 60) for _ in ps]
    for p in ps:
        p.join(10)
    lat = sorted(x for r in parts_out for x in r["latencies"])
    status: dict[str, int] = {}
    for r in parts_out:
        for k, v in r["status"].items():
            status[k] = status.get(k, 0) + v
    ok = status.get("200", 0)
    return {
        "workload": workload,
        "procs": procs,
        "conns_per_proc": conns,
        "warmup_s": warmup,
        "duration_s": duration,
        "requests": len(lat),
        "ok": ok,
        "throughput_rps": ok / duration,
        "status": status,
        "rejected_503": status.get("503", 0),
        "errors": sum(r["errors"] for r in parts_out) + sum(v for k, v in status.items() if k not in ("200", "503")),
        "latency_ms": {
            "p50": percentile(lat, 0.50) * 1000,
            "p95": percentile(lat, 0.95) * 1000,
            "p99": percentile(lat, 0.99) * 1000,
            "mean": (sum(lat) / len(lat) * 1000) if lat else float("nan"),
        },
        "server_cpu_cores": (cpu1 - cpu0) / (t1 - t0),
        "loadgen_runtime": {"python": platform.python_version(), "sys_version": sys.version, "cpu_count": os.cpu_count()},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--workload", choices=WORKLOADS, default="point")
    ap.add_argument("--procs", type=int, default=4)
    ap.add_argument("--conns", type=int, default=8)
    ap.add_argument("--warmup", type=float, default=3.0)
    ap.add_argument("--duration", type=float, default=10.0)
    ap.add_argument("--max-id", type=int, default=1_000_000)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out")
    args = ap.parse_args()
    r = run(args.url, args.workload, procs=args.procs, conns=args.conns, warmup=args.warmup,
            duration=args.duration, max_id=args.max_id, seed=args.seed)  # fmt: skip
    print(json.dumps({k: v for k, v in r.items() if k != "loadgen_runtime"}, indent=2))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(r, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
