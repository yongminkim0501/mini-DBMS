"""psql 호환: 실제 psql 로 서버(서브프로세스)에 접속해 골든 출력과 비교, 그리고 같은 스크립트의 CLI 출력과 비교.

psql 이 없으면 건너뛴다 (CI 는 ubuntu 에서 postgresql-client, macOS 에서 libpq 를 설치한다).
골든 출력(tests/pgwire/psql/*.out)은 같은 스크립트를 실제 PostgreSQL 17 에 실행한 결과와 대조해 검증했다.
차이가 나는 부분(EXPLAIN 의 cost, server_version 등)은 docs/pg-protocol.md "PostgreSQL 과 다른 부분" 에 정리했다.

골든 갱신: ``pytest tests/pgwire/test_psql.py --update-golden``
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import make_db

PSQL = shutil.which("psql")
HERE = Path(__file__).parent / "psql"
SCENARIOS = sorted(p.stem for p in HERE.glob("*.sql"))
# CLI 와 비교할 수 있는 시나리오 (on_error_stop 은 psql 메타 명령을 쓴다)
CONSISTENCY = [s for s in SCENARIOS if s != "on_error_stop"]

pytestmark = pytest.mark.skipif(PSQL is None, reason="psql not installed")


def clean_env() -> dict[str, str]:
    # 로케일(메시지 번역)과 PG* 환경 변수가 출력에 영향을 주지 않게
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG") and k not in ("LANG", "LANGUAGE")}
    env["LC_ALL"] = "C"
    return env


@pytest.fixture
def serve(tmp_path):
    procs = []

    def start() -> int:
        db = make_db(tmp_path / f"db{len(procs)}")
        p = subprocess.Popen(
            [sys.executable, "-m", "minidb", "serve", "--db", str(db), "--pg-port", "0", "--workers", "2",
             "--no-access-log"],
            stderr=subprocess.PIPE, text=True, env=clean_env(),
        )  # fmt: skip
        procs.append(p)
        line = p.stderr.readline()
        m = re.search(r"postgresql://127\.0\.0\.1:(\d+)/minidb", line)
        assert m, line
        return int(m.group(1))

    yield start
    for p in procs:
        p.terminate()
        p.wait(30)
        p.stderr.close()


def run_psql(port: int, script: Path) -> str:
    with script.open("rb") as f:
        r = subprocess.run(
            [PSQL, "-X", "-h", "127.0.0.1", "-p", str(port), "-U", "test", "-d", "minidb", "-f", "-"],
            stdin=f, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=clean_env(), timeout=60,
        )  # fmt: skip
    return r.stdout.decode("utf-8") + f"exit={r.returncode}\n"


@pytest.mark.parametrize("name", SCENARIOS)
def test_psql_golden(serve, name, request):
    out = run_psql(serve(), HERE / f"{name}.sql")
    golden = HERE / f"{name}.out"
    if request.config.getoption("--update-golden"):
        golden.write_text(out, encoding="utf-8")
    assert out == golden.read_text(encoding="utf-8")


_LINE_RE = re.compile(r"^LINE (\d+): (.*)\n( *)\^$", re.M)


def normalize_line_numbers(text: str) -> str:
    """``LINE n:`` 의 n 을 지우고 캐럿 위치를 그에 맞게 옮긴다.

    불가피한 차이: psql 은 문장을 하나씩 서버에 보내므로 서버(와 psql)가 아는 줄 번호는 **문장 안에서의** 번호다.
    CLI 는 스크립트 파일 전체를 보므로 **파일 기준** 줄 번호를 쓴다 (스크립트를 고칠 때 더 쓸모 있다).
    줄 내용과 캐럿이 가리키는 문자는 같아야 한다.
    """

    def repl(m: re.Match[str]) -> str:
        label = f"LINE {m.group(1)}: "
        return f"LINE ?: {m.group(2)}\n{' ' * (len(m.group(3)) - len(label) + len('LINE ?: '))}^"

    return _LINE_RE.sub(repl, text)


def normalize_psql(text: str) -> str:
    text = re.sub(r"^psql:<stdin>:\d+: ", "", text, flags=re.M)  # psql 은 오류 앞에 위치를 붙인다
    return re.sub(r"exit=\d+\n\Z", "", text)


@pytest.mark.parametrize("name", CONSISTENCY)
def test_psql_matches_cli(serve, tmp_path, name):
    """같은 스크립트: psql(+서버) 출력 == python -m minidb run (결과 표와 오류 형식)."""
    script = HERE / f"{name}.sql"
    psql_out = normalize_psql(run_psql(serve(), script))
    db = make_db(tmp_path / "cli")
    r = subprocess.run(
        [sys.executable, "-m", "minidb", "run", str(script), "--db", str(db), "--on-error", "continue"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=clean_env(), timeout=60,
    )  # fmt: skip
    assert normalize_line_numbers(r.stdout.decode("utf-8")) == normalize_line_numbers(psql_out)
