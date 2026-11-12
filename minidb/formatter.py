"""psql 의 aligned 출력 형식.

::

     id | name | age
    ----+------+-----
      1 | kim  |  25
      2 | 홍길동 |  31
    (2 rows)

규칙 (psql 기본값 ``\\pset format aligned``, ``border 1``):
- 헤더는 가운데 정렬. 남는 칸이 홀수면 오른쪽에 하나 더 둔다. 헤더 줄은 마지막 칸도 폭만큼 채운다.
- 숫자(bigint)는 오른쪽 정렬, 문자(text)는 왼쪽 정렬. 마지막 컬럼이 왼쪽 정렬이면 뒤를 공백으로 채우지 않는다.
- NULL 은 빈칸.
- 폭은 화면 표시 폭으로 계산한다. 한글 등 전각 문자는 2칸 (errorfmt 와 같은 :func:`display_width`).
- 값 안의 줄바꿈은 psql 처럼 여러 줄로 나누고, 이어지는 줄이 있는 칸 끝에 ``+`` 를 표시한다.
- 마지막 줄은 ``(1 row)`` / ``(N rows)``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from .result import ColumnInfo
from .sql.errorfmt import display_width

__all__ = ["format_table", "format_cell", "row_count_footer"]


def format_cell(value: Any) -> str:
    """값 하나의 표시 문자열 (NULL 은 빈 문자열)."""
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def row_count_footer(n: int) -> str:
    """``(1 row)`` / ``(N rows)``."""
    return "(1 row)" if n == 1 else f"({n} rows)"


def _center(text: str, width: int) -> str:
    pad = width - display_width(text)
    left = pad // 2
    return " " * left + text + " " * (pad - left)


def format_table(columns: Sequence[ColumnInfo], rows: Iterable[Sequence[Any]]) -> list[str]:
    """결과를 psql aligned 형식의 줄 목록으로 만든다 (마지막 줄은 행 수 표시).

    폭을 정하려면 모든 행을 봐야 하므로 rows 는 여기서 모두 읽는다 (psql 도 결과 전체를 받은 뒤 출력한다).
    """
    headers = [c.name for c in columns]
    numeric = [c.is_numeric for c in columns]
    ncol = len(columns)
    # 각 칸을 줄 단위로 쪼개 둔다 (값 안의 줄바꿈 처리)
    cells: list[list[list[str]]] = [[format_cell(v).split("\n") for v in row] for row in rows]

    widths = [display_width(h) for h in headers]
    for row in cells:
        for j, lines in enumerate(row):
            for ln in lines:
                w = display_width(ln)
                if w > widths[j]:
                    widths[j] = w

    out = [" " + " | ".join(_center(h, widths[j]) for j, h in enumerate(headers)) + " "]
    out.append("+".join("-" * (w + 2) for w in widths))

    last = ncol - 1
    for row in cells:
        height = max(len(lines) for lines in row)
        for li in range(height):
            parts = [" "]
            for j, lines in enumerate(row):
                text = lines[li] if li < len(lines) else ""
                more = li < len(lines) - 1  # 이 칸에 다음 줄이 있으면 '+' 표시
                pad = widths[j] - display_width(text)
                if numeric[j]:
                    cell = " " * pad + text
                elif j < last or more:
                    cell = text + " " * pad
                else:
                    cell = text  # 마지막 왼쪽 정렬 칸은 뒤를 채우지 않는다
                parts.append(cell)
                if j < last:
                    parts.append("+| " if more else " | ")
                elif more:
                    parts.append("+")
            out.append("".join(parts))

    out.append(row_count_footer(len(cells)))
    return out
