"""results/server/*.json → docs/images/*.svg 선 그래프 (표준 라이브러리만).

워크로드마다 그래프 하나: x = 워커 수, y = 처리량(req/s, 3회 반복의 중앙값), 선 = 서버 조건
(3.14.6 GIL / 3.14.6t / 3.14.6t + PYTHON_GIL=1). 실제 사용 코어 수 그래프도 함께 만든다.

사용 예::

    python scripts/plot_results.py --results results/server --out docs/images
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from html import escape
from pathlib import Path
from typing import Any

CONDITIONS = [
    ("gil", "3.14.6 (GIL)", "#2563eb", "0"),
    ("ft", "3.14.6t (free-threaded)", "#dc2626", "0"),
    ("ft_gil1", "3.14.6t + PYTHON_GIL=1", "#6b7280", "6 4"),
]
WORKLOAD_TITLES = {
    "point": "(a) id 점 검색 — 100만 건, Index Scan",
    "scan": "(b) Seq Scan — 10만 건, name 조건",
    "insert": "(c) 단건 INSERT",
    "mixed": "(d) 혼합: 점 검색 90% + INSERT 10%",
}


def load(results_dir: Path) -> list[dict[str, Any]]:
    rows = []
    for p in sorted(results_dir.glob("*.json")):
        if p.name == "summary.json":
            continue
        rows.append(json.loads(p.read_text(encoding="utf-8")))
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[tuple[str, str, int], dict[str, float]]:
    """(조건, 워크로드, 워커 수) → 반복들의 중앙값."""
    groups: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault((r["condition"], r["workload"], r["workers"]), []).append(r)
    out = {}
    for k, rs in groups.items():
        out[k] = {
            "throughput_rps": statistics.median(x["throughput_rps"] for x in rs),
            "p50_ms": statistics.median(x["latency_ms"]["p50"] for x in rs),
            "p95_ms": statistics.median(x["latency_ms"]["p95"] for x in rs),
            "p99_ms": statistics.median(x["latency_ms"]["p99"] for x in rs),
            "server_cpu_cores": statistics.median(x["server_cpu_cores"] for x in rs),
            "rejected_503": sum(x["rejected_503"] for x in rs),
            "errors": sum(x["errors"] for x in rs),
            "runs": len(rs),
        }
    return out


def _nice_max(v: float) -> float:
    if v <= 0:
        return 1.0
    mag = 10 ** (len(str(int(v))) - 1)
    for m in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if v <= m * mag:
            return m * mag
    return 10 * mag


def line_chart(title: str, xs: list[int], series: list[tuple[str, str, str, list[float | None]]], y_label: str) -> str:
    W, H = 640, 400
    L, R, T, B = 70, 20, 50, 90
    pw, ph = W - L - R, H - T - B
    ymax = _nice_max(max((v for _, _, _, ys in series for v in ys if v is not None), default=1.0) * 1.05)
    xi = {x: i for i, x in enumerate(xs)}

    def px(x: int) -> float:
        return L + (pw * xi[x] / max(1, len(xs) - 1))

    def py(y: float) -> float:
        return T + ph - ph * y / ymax

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
        'font-family="-apple-system, Segoe UI, Helvetica, Arial, sans-serif" font-size="12">',
        f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
        f'<text x="{W / 2}" y="24" text-anchor="middle" font-size="15" font-weight="600" fill="#111">{escape(title)}</text>',
    ]
    for i in range(6):  # 가로 격자와 y 눈금
        y = ymax * i / 5
        yy = py(y)
        out.append(f'<line x1="{L}" y1="{yy:.1f}" x2="{L + pw}" y2="{yy:.1f}" stroke="#e5e7eb"/>')
        label = f"{y:,.0f}" if ymax >= 10 else f"{y:.1f}"
        out.append(f'<text x="{L - 8}" y="{yy + 4:.1f}" text-anchor="end" fill="#374151">{label}</text>')
    for x in xs:
        xx = px(x)
        out.append(f'<line x1="{xx:.1f}" y1="{T + ph}" x2="{xx:.1f}" y2="{T + ph + 5}" stroke="#374151"/>')
        out.append(f'<text x="{xx:.1f}" y="{T + ph + 20}" text-anchor="middle" fill="#374151">{x}</text>')
    out.append(f'<line x1="{L}" y1="{T + ph}" x2="{L + pw}" y2="{T + ph}" stroke="#374151"/>')
    out.append(f'<line x1="{L}" y1="{T}" x2="{L}" y2="{T + ph}" stroke="#374151"/>')
    out.append(f'<text x="{L + pw / 2}" y="{T + ph + 40}" text-anchor="middle" fill="#111">워커 수</text>')
    out.append(
        f'<text x="18" y="{T + ph / 2}" text-anchor="middle" fill="#111" transform="rotate(-90 18 {T + ph / 2})">'
        f"{escape(y_label)}</text>"
    )
    for name, color, dash, ys in series:
        pts = [(px(x), py(y)) for x, y in zip(xs, ys) if y is not None]
        if not pts:
            continue
        d = " ".join(f"{a:.1f},{b:.1f}" for a, b in pts)
        dash_attr = f' stroke-dasharray="{dash}"' if dash != "0" else ""
        out.append(f'<polyline points="{d}" fill="none" stroke="{color}" stroke-width="2.5"{dash_attr}/>')
        for a, b in pts:
            out.append(f'<circle cx="{a:.1f}" cy="{b:.1f}" r="3.5" fill="{color}"/>')
    lx = L
    for name, color, dash, _ in series:  # 범례
        ly = H - 22
        dash_attr = f' stroke-dasharray="{dash}"' if dash != "0" else ""
        out.append(f'<line x1="{lx}" y1="{ly}" x2="{lx + 24}" y2="{ly}" stroke="{color}" stroke-width="2.5"{dash_attr}/>')
        out.append(f'<text x="{lx + 30}" y="{ly + 4}" fill="#111">{escape(name)}</text>')
        lx += 30 + 8 * len(name) + 24
    out.append("</svg>")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--results", default="results/server")
    ap.add_argument("--out", default="docs/images")
    args = ap.parse_args()
    rows = load(Path(args.results))
    if not rows:
        print("no results", file=sys.stderr)
        return 1
    summ = summarize(rows)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    xs = sorted({k[2] for k in summ})
    workloads = [w for w in WORKLOAD_TITLES if any(k[1] == w for k in summ)]
    for w in workloads:
        for metric, label, suffix in (
            ("throughput_rps", "처리량 (req/s, 3회 중앙값)", ""),
            ("server_cpu_cores", "서버가 실제로 쓴 코어 수", "_cpu"),
        ):
            series = []
            for cond, name, color, dash in CONDITIONS:
                ys = [summ.get((cond, w, x), {}).get(metric) for x in xs]
                series.append((name, color, dash, ys))
            title = WORKLOAD_TITLES[w] + ("" if not suffix else " — 사용 코어")
            path = out_dir / f"phase3_{w}{suffix}.svg"
            path.write_text(line_chart(title, xs, series, label), encoding="utf-8")
            print(f"wrote {path}")
    summary = {f"{c}|{w}|{n}": v for (c, w, n), v in sorted(summ.items())}
    (Path(args.results) / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
