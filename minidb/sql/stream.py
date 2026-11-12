"""큰 SQL 스크립트를 메모리에 다 올리지 않고 문장 단위로 읽는 스트리밍 분리기.

:func:`~minidb.sql.lexer.iter_statements` 는 문자열 전체가 필요하다. CLI 가 100만 행 스크립트를 실행할 때 메모리가
일정하도록, 이 모듈은 파일을 청크로 읽으면서 **세미콜론 토큰으로 끝난 문장만** 확정해 내보내고, 확정된 부분은
버퍼에서 버린다. 버퍼 끝의 아직 끝나지 않은 문장은 다음 청크와 이어 붙여 다시 렉싱한다.

- 위치: 토큰과 오류의 position 은 :func:`iter_tokens` 의 ``base_offset`` 으로 **파일 전체 기준**을 유지한다.
- 줄 번호: 버린 부분의 줄바꿈 수를 세어 두므로 오류 출력의 ``LINE n`` 도 파일 전체 기준이다.
- 렉싱 오류: 청크 경계에서 잘린 것일 수 있으므로(닫는 따옴표가 다음 청크에 있음) 바로 확정하지 않는다.
  ``unterminated ...`` 오류이거나 오류 위치가 버퍼 끝 가까이면 더 읽어 본다. 파일 끝에서도 남아 있으면
  진짜 오류로 내보내고 멈춘다 (오류 이후는 해석할 수 없음 — split_statements 와 같은 규칙).
- 한 문장이 청크보다 크면 읽는 크기를 두 배씩 늘려, 같은 부분을 반복 렉싱하는 비용이 전체 크기에 비례하게 한다.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TextIO

from ..errors import DatabaseError
from .lexer import StatementSource, iter_tokens
from .tokens import Token, TokenType

__all__ = ["ScriptStatement", "iter_script_statements"]

_DEFAULT_CHUNK = 1 << 20  # 1 MiB (문자 수)
_LOOKAHEAD = 64  # 버퍼 끝에서 이만큼 안쪽의 오류는 청크 경계 때문일 수 있다


@dataclass(frozen=True, slots=True)
class ScriptStatement:
    """스크립트의 문장 하나와 오류 출력에 필요한 위치 정보.

    Attributes:
        source: 문장 (start_offset, 토큰, 오류 위치 모두 파일 전체 기준).
        line: 문장이 시작하는 줄 번호 (1부터, 파일 전체 기준).
        line_prefix: 문장이 시작하는 줄에서 문장 앞에 있는 텍스트.
            ``line_prefix + source.text`` 를 :func:`~minidb.sql.errorfmt.format_error` 에 넘기면
            ``LINE n:`` 에 그 줄 전체가 보인다.
    """

    source: StatementSource
    line: int
    line_prefix: str

    @property
    def context(self) -> str:
        """오류 출력용 텍스트 (문장이 시작하는 줄의 처음부터 문장 끝까지)."""
        return self.line_prefix + self.source.text

    @property
    def context_offset(self) -> int:
        """``context[0]`` 의 파일 전체 기준 위치 (format_error 의 position_offset)."""
        return self.source.start_offset - len(self.line_prefix)


def iter_script_statements(reader: TextIO, *, chunk_size: int = _DEFAULT_CHUNK) -> Iterator[ScriptStatement]:
    """``reader`` 에서 SQL 을 청크 단위로 읽어 문장을 하나씩 내보낸다.

    공백·주석만 있는 문장은 내보내지 않는다. 렉싱 오류가 확정되면 마지막 원소로
    ``source.error`` 가 있는 문장을 내보내고 끝난다.
    """
    SEMI, EOF = TokenType.SEMICOLON, TokenType.EOF
    buf = ""
    buf_start = 0  # buf[0] 의 파일 전체 기준 위치
    lines_before = 0  # buf_start 앞에 있는 줄바꿈 수
    line_head = ""  # buf_start 가 속한 줄에서 buf_start 앞의 텍스트
    eof = False
    want = chunk_size

    while True:
        if not eof:
            chunk = reader.read(want)
            if chunk:
                buf += chunk
            else:
                eof = True

        # 버퍼를 렉싱해 세미콜론으로 끝난 문장들을 모은다.
        done: list[tuple[int, int, tuple[Token, ...]]] = []  # (로컬 시작, 로컬 끝, 토큰)
        cut = 0  # 여기까지(로컬)는 확정되어 버려도 된다
        cur: list[Token] = []
        error: DatabaseError | None = None
        try:
            for tok in iter_tokens(buf, base_offset=buf_start):
                tt = tok.type
                if tt is SEMI or (tt is EOF and eof):
                    if cur:
                        done.append((cur[0].offset - buf_start, cur[-1].end - buf_start, tuple(cur)))
                        cur = []
                    cut = tok.end - buf_start
                elif tt is not EOF:
                    cur.append(tok)
        except DatabaseError as exc:
            local = (exc.position - 1 - buf_start) if exc.position else len(buf)
            maybe_truncated = exc.message.startswith("unterminated") or local + _LOOKAHEAD >= len(buf)
            if eof or not maybe_truncated:
                error = exc

        # 확정된 문장 내보내기 (줄 번호는 앞에서부터 차례로 센다)
        scan_pos, scan_lines, head = 0, lines_before, line_head
        for start, end, toks in done:
            seg = buf[scan_pos:start]
            nl = seg.count("\n")
            head = seg[seg.rfind("\n") + 1 :] if nl else head + seg
            scan_lines += nl
            scan_pos = start
            src = StatementSource(buf[start:end], buf_start + start, None, toks)
            yield ScriptStatement(src, scan_lines + 1, head)

        if error is not None:
            start = (cur[0].offset - buf_start) if cur else max(0, (error.position or 1) - 1 - buf_start)
            seg = buf[scan_pos:start]
            nl = seg.count("\n")
            head = seg[seg.rfind("\n") + 1 :] if nl else head + seg
            src = StatementSource(buf[start:], buf_start + start, error)
            yield ScriptStatement(src, scan_lines + nl + 1, head)
            return
        if eof:
            return

        # 확정된 앞부분을 버린다.
        if cut:
            consumed = buf[:cut]
            nl = consumed.count("\n")
            lines_before += nl
            line_head = consumed[consumed.rfind("\n") + 1 :] if nl else line_head + consumed
            buf = buf[cut:]
            buf_start += cut
            want = chunk_size
        else:
            # 문장 하나가 버퍼보다 크다 → 읽는 양을 늘려 반복 렉싱 비용을 제한한다.
            want = max(chunk_size, len(buf))
