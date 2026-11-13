"""golden 테스트: tests/golden/<name>.sql 을 CLI 로 실행한 출력(stdout+stderr, 순서 유지)을 <name>.out 과 비교한다.

- .sql 첫 줄의 ``-- args: ...`` 는 CLI 옵션 (예: ``--on-error continue``).
- 각 시나리오는 tests/golden/schema 를 복사한 빈 DB 에서 실행한다.
- 기대 출력 갱신: ``UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py``
"""

import io
import os
import shlex
import shutil
from pathlib import Path

import pytest

from minidb.__main__ import main

GOLDEN = Path(__file__).parent / "golden"
CASES = sorted(p.stem for p in GOLDEN.glob("*.sql"))


def run_case(name: str, tmp_path: Path) -> str:
    sql_path = GOLDEN / f"{name}.sql"
    first = sql_path.read_text(encoding="utf-8").splitlines()[0]
    extra = shlex.split(first.removeprefix("-- args:")) if first.startswith("-- args:") else []
    db = tmp_path / "db"
    shutil.copytree(GOLDEN / "schema", db / "schema")
    buf = io.StringIO()
    code = main(["run", str(sql_path), "--db", str(db), *extra], stdout=buf, stderr=buf)
    return buf.getvalue() + f"[exit {code}]\n"


@pytest.mark.parametrize("name", CASES)
def test_golden(name, tmp_path):
    actual = run_case(name, tmp_path)
    out_path = GOLDEN / f"{name}.out"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        out_path.write_text(actual, encoding="utf-8", newline="")
    assert out_path.exists(), f"{out_path.name} 이 없습니다. UPDATE_GOLDEN=1 로 생성하세요."
    assert actual == out_path.read_text(encoding="utf-8")


def test_minimum_scenarios_present():
    required = {
        "basic", "multirow_returning", "null_default", "where_ops", "type_errors",
        "continue_mixed", "korean", "explain", "quoted_tables", "index_explain", "set_show", "no_index",
    }  # fmt: skip
    assert required <= set(CASES)
