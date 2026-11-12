"""psql 스타일 에러 출력.

::

    ERROR:  syntax error at or near "FORM"
    LINE 1: SELECT * FORM users;
                     ^

캐럿(^) 위치는 **화면 표시 폭** 기준으로 계산한다. 한글 같은 동아시아 전각 문자는 터미널에서
두 칸을 차지하므로(unicodedata.east_asian_width 가 W/F) 폭 2로, 결합 문자는 폭 0으로 센다.
탭은 터미널마다 폭이 달라 캐럿이 어긋나므로 공백 하나로 바꿔 출력한다.
긴 줄은 psql(libpq reportErrorPosition)처럼 캐럿 주변 약 60칸만 남기고 앞뒤에 "..." 를 붙인다.
"""

from __future__ import annotations

import unicodedata

from ..errors import DatabaseError

__all__ = ["format_error", "display_width"]

_DISPLAY_SIZE = 60  # 잘라낸 줄의 최대 표시 폭 (libpq 와 같은 값)
_MIN_RIGHT_CUT = 10  # 캐럿 오른쪽에 최소한 남길 폭 (libpq 와 같은 값)
_ELLIPSIS = "..."


def _char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def display_width(s: str) -> int:
    """문자열의 터미널 표시 폭 (전각 2, 결합 문자 0, 나머지 1)."""
    return sum(_char_width(ch) for ch in s)


def format_error(sql: str, err: DatabaseError, *, position_offset: int = 0, line_offset: int = 0) -> str:
    """``err`` 를 psql 과 같은 형식의 여러 줄 문자열로 만든다.

    출력 순서는 libpq 와 같다: ``ERROR:`` → (position 이 있으면) ``LINE n:`` 과 캐럿 → ``DETAIL:`` → ``HINT:``.
    ``err.position`` 이 없으면 LINE/캐럿 없이 메시지(와 DETAIL/HINT)만 출력한다.
    여러 줄 SQL 이면 position 이 속한 줄 번호와 그 줄만 보여준다.

    Args:
        sql: 위치 계산에 쓸 SQL 텍스트. 스크립트 전체를 메모리에 두지 않는 경우(CLI 스트리밍)에는
            오류 문장이 있는 부분만 넘기고 아래 두 오프셋으로 보정한다.
        position_offset: ``sql[0]`` 의 원문 기준 위치(0부터). ``err.position - position_offset`` 이 ``sql`` 안 위치.
        line_offset: ``sql`` 첫 줄 앞에 있는 줄 수. ``LINE`` 번호에 더해진다.
    """
    lines = [f"ERROR:  {err.message}"]
    if err.position is not None:
        lines.append(_position_lines(sql, err.position - position_offset, line_offset))
    if err.detail:
        lines.append(f"DETAIL:  {err.detail}")
    if err.hint:
        lines.append(f"HINT:  {err.hint}")
    return "\n".join(lines)


def _position_lines(sql: str, position: int, line_offset: int) -> str:
    """``LINE n: ...`` 줄과 캐럿 줄."""

    # position(1부터) → 0부터의 문자 인덱스. 'end of input' 은 len(sql)+1 일 수 있으므로 끝으로 자른다.
    idx = min(max(position - 1, 0), len(sql))
    line_start = sql.rfind("\n", 0, idx) + 1
    line_end = sql.find("\n", idx)
    if line_end == -1:
        line_end = len(sql)
    line_no = line_offset + sql.count("\n", 0, idx) + 1
    line = sql[line_start:line_end].rstrip("\r").replace("\t", " ")
    col = min(idx - line_start, len(line))  # 줄 안에서 캐럿이 가리키는 문자 인덱스

    widths = [_char_width(ch) for ch in line]
    caret_w = sum(widths[:col])  # 캐럿 앞까지의 표시 폭
    total_w = sum(widths)

    start, end = 0, len(line)
    if total_w > _DISPLAY_SIZE:
        if caret_w + _MIN_RIGHT_CUT < _DISPLAY_SIZE:
            # 캐럿이 앞쪽에 있으면 뒤만 자른다.
            acc = 0
            end = 0
            while end < len(line) and acc + widths[end] <= _DISPLAY_SIZE:
                acc += widths[end]
                end += 1
        else:
            # 캐럿 오른쪽으로 MIN_RIGHT_CUT 폭만큼 남기고, 거기서 왼쪽으로 DISPLAY_SIZE 폭만큼 보여준다.
            acc = caret_w
            end = col
            while end < len(line) and acc + widths[end] <= caret_w + _MIN_RIGHT_CUT:
                acc += widths[end]
                end += 1
            start = end
            acc = 0
            while start > 0 and acc + widths[start - 1] <= _DISPLAY_SIZE:
                start -= 1
                acc += widths[start]

    shown = line[start:end]
    prefix = _ELLIPSIS if start > 0 else ""
    suffix = _ELLIPSIS if end < len(line) else ""
    label = f"LINE {line_no}: "
    # 캐럿 칸 = 라벨 폭 + 앞 생략 기호 폭 + (보이는 구간 시작 ~ 캐럿) 표시 폭
    pad = display_width(label) + len(prefix) + sum(widths[start:col])
    return f"{label}{prefix}{shown}{suffix}\n{' ' * pad}^"
