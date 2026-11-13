"""psycopg2 / psycopg(3) 호환 확인 (수동 실행, pytest 아님).

C 확장 드라이버는 free-threaded 빌드(3.14t)에서 GIL 을 다시 켤 수 있으므로 pytest 프로세스에서 import 하지 않는다.
이 스크립트는 임시 DB 로 서버를 **별도 프로세스**로 띄우고, 이 프로세스에서 드라이버로 접속해 기능을 확인한다.

실행 (드라이버는 실행할 때만 임시로 설치)::

    uv run --python 3.14.6+gil --with psycopg2-binary python scripts/compat/check_psycopg.py
    uv run --python 3.14.6+gil --with "psycopg[binary]" python scripts/compat/check_psycopg.py

결과는 docs/pg-protocol.md "드라이버 호환" 에 기록했다.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

USERS = {
    "oid": 16384,
    "table": "users",
    "columns": [
        {"name": "id", "type": "BIGINT", "auto_increment": True},
        {"name": "name", "type": "TEXT", "nullable": True},
        {"name": "age", "type": "BIGINT", "nullable": True},
    ],
}
ROOT = Path(__file__).resolve().parents[2]


def start_server(db: Path) -> tuple[subprocess.Popen, int]:
    p = subprocess.Popen(
        [sys.executable, "-m", "minidb", "serve", "--db", str(db), "--pg-port", "0", "--no-access-log"],
        stderr=subprocess.PIPE, text=True, cwd=ROOT,
    )  # fmt: skip
    line = p.stderr.readline()
    m = re.search(r"postgresql://127\.0\.0\.1:(\d+)/", line)
    if not m:
        p.kill()
        raise SystemExit(f"server did not start: {line}")
    return p, int(m.group(1))


def check(name: str, fn) -> bool:  # noqa: ANN001
    try:
        detail = fn()
        print(f"  OK    {name}" + (f": {detail}" if detail is not None else ""))
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL  {name}: {type(exc).__name__}: {exc}".rstrip())
        return False


def run_psycopg2(port: int) -> list[bool]:
    import psycopg2  # noqa: PLC0415
    import psycopg2.errors  # noqa: PLC0415

    print(f"psycopg2 {psycopg2.__version__} (libpq {psycopg2.extensions.libpq_version()})")
    conn = psycopg2.connect(host="127.0.0.1", port=port, user="test", dbname="minidb", application_name="check")
    conn.autocommit = True  # 트랜잭션(BEGIN)은 지원하지 않는다
    cur = conn.cursor()
    results = [
        check("server_version", lambda: conn.server_version),
        check("insert returning (client-side params)", lambda: (
            cur.execute("INSERT INTO users (name, age) VALUES (%s, %s) RETURNING id, name", ("홍길동", 2**62)),
            cur.fetchall())[1]),
        check("select with params", lambda: (cur.execute("SELECT name, age FROM users WHERE id = %s", (1,)),
                                             cur.fetchall())[1]),
        check("NULL", lambda: (cur.execute("INSERT INTO users (name) VALUES (%s) RETURNING age", (None,)),
                               cur.fetchall())[1]),
        check("description type OIDs", lambda: (cur.execute("SELECT id, name FROM users"),
                                                [(d.name, d.type_code) for d in cur.description])[1]),
    ]

    def err():
        try:
            cur.execute("SELECT * FROM nope")
        except psycopg2.errors.UndefinedTable as e:
            return f"{e.pgcode} {e.diag.message_primary!r} position={e.diag.statement_position}"
        raise AssertionError("no error")

    results.append(check("error -> psycopg2.errors.UndefinedTable", err))
    results.append(check("transaction mode (autocommit off) -> BEGIN unsupported", lambda: _txn_psycopg2(port)))
    conn.close()
    return results


def _txn_psycopg2(port: int) -> str:
    import psycopg2  # noqa: PLC0415

    c = psycopg2.connect(host="127.0.0.1", port=port, user="test", dbname="minidb")
    try:
        c.cursor().execute("SELECT 1 FROM users")
    except psycopg2.Error as e:
        return f"expected error: {e.pgcode} {e.diag.message_primary}"
    finally:
        c.close()
    raise AssertionError("BEGIN unexpectedly succeeded")


def run_psycopg3(port: int) -> list[bool]:
    import psycopg  # noqa: PLC0415

    print(f"psycopg {psycopg.__version__} (libpq {psycopg.pq.version()}, impl {psycopg.pq.__impl__})")
    conn = psycopg.connect(host="127.0.0.1", port=port, user="test", dbname="minidb", autocommit=True)
    results = [
        check("server-side params (extended protocol)", lambda: conn.execute(
            "INSERT INTO users (name, age) VALUES (%s, %s) RETURNING id, name, age", ("kim", 25)).fetchall()),
        check("select by id", lambda: conn.execute("SELECT name FROM users WHERE id = %s", (1,)).fetchall()),
        check("prepared (prepare=True)", lambda: [
            conn.execute("SELECT name FROM users WHERE id = %s", (1,), prepare=True).fetchone() for _ in range(3)]),
        check("binary results", lambda: conn.cursor(binary=True).execute(
            "SELECT id, name FROM users WHERE id = %s", (1,)).fetchall()),
        check("NULL param", lambda: conn.execute("SELECT * FROM users WHERE age = %s", (None,)).fetchall()),
    ]

    def err():
        try:
            conn.execute("SELECT * FROM users WHERE name = %s", (5,))
        except psycopg.Error as e:
            return f"{e.sqlstate} {e.diag.message_primary!r}"
        raise AssertionError("no error")

    results.append(check("type error (int param vs text column)", err))
    conn.close()
    return results


def main() -> int:
    gil = getattr(sys, "_is_gil_enabled", lambda: True)
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "db"
        (db / "schema").mkdir(parents=True)
        (db / "schema" / "16384.json").write_text(json.dumps(USERS), encoding="utf-8")
        server, port = start_server(db)
        try:
            ok: list[bool] = []
            ran = False
            for mod, fn in (("psycopg2", run_psycopg2), ("psycopg", run_psycopg3)):
                try:
                    __import__(mod)
                except ImportError:
                    print(f"{mod}: not installed (skip)")
                    continue
                ran = True
                ok += fn(port)
            print(f"python {sys.version.split()[0]}, GIL enabled after driver import: {gil()}")
            if not ran:
                print("no driver installed; see the module docstring for how to run")
                return 2
            print(f"{sum(ok)}/{len(ok)} checks passed")
            return 0 if all(ok) else 1
        finally:
            server.terminate()
            server.wait(30)


if __name__ == "__main__":
    sys.exit(main())
