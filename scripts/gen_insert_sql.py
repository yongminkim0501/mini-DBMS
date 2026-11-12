"""users 테이블용 다중 행 INSERT 스크립트 생성기 (Phase 2 대량 데이터 준비).

사용 예::

    python scripts/gen_insert_sql.py --rows 1000000 --batch 5000 --seed 42 --out data/users_1m.sql

- 고정 시드라 같은 인자면 항상 같은 파일이 나온다.
- name: 한글/영문 혼합 2~12자 (10% NULL, 가끔 이스케이프가 필요한 작은따옴표 포함), age: 0~100 (10% NULL).
- 배치마다 ``INSERT INTO users (name, age) VALUES (...), (...);`` 한 문장. 행마다 줄을 바꾼다.
- 파일 전체를 메모리에 만들지 않고 배치 단위로 쓴다.
"""

from __future__ import annotations

import argparse
import random
import sys
from collections.abc import Iterator
from pathlib import Path

_HANGUL = [chr(c) for c in range(0xAC00, 0xD7A4)]
_ASCII = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def gen_name(rng: random.Random) -> str | None:
    """이름 하나 (10% 는 None)."""
    if rng.random() < 0.10:
        return None
    n = rng.randint(2, 12)
    ratio = rng.random()
    s = "".join(rng.choice(_HANGUL) if rng.random() < ratio else rng.choice(_ASCII) for _ in range(n))
    if rng.random() < 0.02:
        s = s[:1] + "'" + s[1:]
    return s


def gen_age(rng: random.Random) -> int | None:
    """나이 하나 (10% 는 None)."""
    return None if rng.random() < 0.10 else rng.randint(0, 100)


def sql_literal(v: str | int | None) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, int):
        return str(v)
    return "'" + v.replace("'", "''") + "'"


def iter_batches(rows: int, batch: int, seed: int) -> Iterator[str]:
    """INSERT 문(세미콜론과 줄바꿈 포함)을 배치마다 하나씩 만든다."""
    rng = random.Random(seed)
    done = 0
    while done < rows:
        n = min(batch, rows - done)
        values = ",\n".join(f"({sql_literal(gen_name(rng))}, {sql_literal(gen_age(rng))})" for _ in range(n))
        yield f"INSERT INTO users (name, age) VALUES\n{values};\n"
        done += n


def write_script(path: str | Path, rows: int, batch: int, seed: int) -> Path:
    """스크립트를 ``path`` 에 쓰고 경로를 반환한다."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        for stmt in iter_batches(rows, batch, seed):
            f.write(stmt)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rows", type=int, default=1_000_000)
    ap.add_argument("--batch", type=int, default=5_000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="data/users_1m.sql")
    args = ap.parse_args(argv)
    if args.rows < 0 or args.batch <= 0:
        ap.error("--rows must be >= 0 and --batch must be > 0")
    path = write_script(args.out, args.rows, args.batch, args.seed)
    print(f"wrote {args.rows:,} rows in {-(-args.rows // args.batch):,} statements to {path} ({path.stat().st_size / 2**20:.1f} MiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
