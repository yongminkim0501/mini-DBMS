"""SQL 렉서: 텍스트 → 토큰, 그리고 토큰 기반 문장 분리.

구현 방식: 모든 토큰 규칙을 이름 있는 그룹의 alternation 으로 묶은 **컴파일된 정규식 하나**로
현재 위치에서 ``match`` 하고, ``m.lastgroup`` (어느 규칙이 맞았는지)으로 분기한다.
정규식 엔진(C)이 "어떤 토큰인가"를 한 번에 판단하므로 파이썬 수준 분기가 최소화된다.
정규식으로 표현할 수 없는 중첩 블록 주석만 수동으로 스캔한다.

모듈 수준에는 컴파일된 패턴과 불변 상수만 있다 (가변 상태 없음 → 스레드 간 공유 안전).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field

from ..errors import DatabaseError, FeatureNotSupported, NameTooLong, SqlSyntaxError
from .tokens import RESERVED_KEYWORDS, Token, TokenType

__all__ = ["MAX_IDENTIFIER_BYTES", "StatementSource", "iter_tokens", "tokenize", "iter_statements", "split_statements"]

#: 식별자 최대 길이 (UTF-8 바이트). PostgreSQL 의 NAMEDATALEN - 1.
MAX_IDENTIFIER_BYTES = 63

# 토큰 규칙. 순서가 중요하다: 앞의 대안이 먼저 시도된다.
#  - 주석('--', '/*')은 기호 '-' 보다 앞에
#  - 접두사 문자열(E'..' 등)은 식별자보다 앞에 (E 가 식별자로 먹히지 않도록)
#  - 소수/지수 숫자는 정수보다 앞에
#  - 마지막의 other 는 어떤 문자든 하나 매치 → 모든 위치에서 매치가 보장된다
_TOKEN_PATTERN = r"""
  (?P<ws>[ \t\r\n]+)
| (?P<line_comment>--[^\n]*)
| (?P<block_comment>/\*)
| (?P<prefixed_string>(?:[eEbBxXnN]|[uU]&)')
| (?P<unicode_ident>[uU]&")
| (?P<ident>[A-Za-z_][A-Za-z0-9_$]*)
| (?P<qident>"[^"]*(?:""[^"]*)*"?)
| (?P<string>'[^']*(?:''[^']*)*'?)
| (?P<numeric>(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|[0-9]+[eE][+-]?[0-9]+)
| (?P<integer>[0-9]+)
| (?P<param>\$[0-9]+)
| (?P<dollar_quote>\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$)
| (?P<op2><>|!=|<=|>=)
| (?P<op1>[(),;*.=<>\-])
| (?P<other>.)
"""
_TOKEN_RE = re.compile(_TOKEN_PATTERN, re.VERBOSE | re.DOTALL)
# 숫자 바로 뒤에 식별자 문자가 붙은 경우 (예: 123abc). PostgreSQL 15+ 와 같이 오류로 처리.
_TRAILING_JUNK_RE = re.compile(r"[A-Za-z_]")
# 블록 주석 안에서 다음 '/*' 또는 '*/' 를 찾는다.
_COMMENT_DELIM_RE = re.compile(r"/\*|\*/")

_OP_TYPES: dict[str, TokenType] = {
    "(": TokenType.LPAREN,
    ")": TokenType.RPAREN,
    ",": TokenType.COMMA,
    ";": TokenType.SEMICOLON,
    "*": TokenType.STAR,
    ".": TokenType.DOT,
    "=": TokenType.EQ,
    "<": TokenType.LT,
    ">": TokenType.GT,
    "-": TokenType.MINUS,
    "<>": TokenType.NE,
    "!=": TokenType.NE,
    "<=": TokenType.LE,
    ">=": TokenType.GE,
}
# 소문자 → KEYWORD 값(대문자). dict 조회 한 번으로 "키워드인가 + 정규화 값"을 얻는다.
_KEYWORDS: dict[str, str] = {k.lower(): k for k in RESERVED_KEYWORDS}


def _check_ident_length(name: str, offset: int) -> None:
    if len(name) > MAX_IDENTIFIER_BYTES // 4 and len(name.encode("utf-8")) > MAX_IDENTIFIER_BYTES:
        # PostgreSQL 은 잘라내고 NOTICE 를 내지만, minidb 는 조용한 이름 변경을 피하려고 오류로 처리한다.
        raise NameTooLong(
            f'identifier "{name}" is too long ({len(name.encode("utf-8"))} bytes, maximum is {MAX_IDENTIFIER_BYTES})',
            position=offset + 1,
        )


def iter_tokens(text: str) -> Iterator[Token]:
    """``text`` 를 토큰으로 쪼개 하나씩 내보낸다. 마지막은 항상 EOF 토큰.

    공백과 주석은 토큰을 만들지 않는다. 오류(닫히지 않은 문자열 등)는 그 지점에서
    예외로 올라가며, 그 전에 내보낸 토큰은 유효하다 (split_statements 가 이 성질을 이용한다).

    Raises:
        SqlSyntaxError, FeatureNotSupported, NameTooLong: 렉싱 오류. ``position`` 은 ``offset + 1``.
    """
    match = _TOKEN_RE.match
    n = len(text)
    pos = 0
    line = 1
    line_start = 0  # 현재 줄의 시작 offset → column = offset - line_start + 1
    T = TokenType
    keywords = _KEYWORDS
    op_types = _OP_TYPES
    while pos < n:
        m = match(text, pos)
        assert m is not None  # (?P<other>.) 덕분에 항상 매치
        kind = m.lastgroup
        end = m.end()

        if kind == "ident":
            word = m.group()
            low = word.lower()
            kw = keywords.get(low)
            if kw is not None:
                yield Token(T.KEYWORD, kw, pos, end, line, pos - line_start + 1)
            else:
                _check_ident_length(low, pos)
                yield Token(T.IDENT, low, pos, end, line, pos - line_start + 1)
            pos = end
            continue

        if kind == "ws":
            # 줄바꿈이 있으면 줄 번호와 줄 시작 위치를 갱신한다.
            nl = text.count("\n", pos, end)
            if nl:
                line += nl
                line_start = text.rfind("\n", pos, end) + 1
            pos = end
            continue

        if kind == "op1" or kind == "op2":
            sym = m.group()
            yield Token(op_types[sym], sym, pos, end, line, pos - line_start + 1)
            pos = end
            continue

        if kind == "integer":
            if end < n and _TRAILING_JUNK_RE.match(text, end):
                junk_end = _TOKEN_RE.match(text, end).end()  # 붙어 있는 식별자 끝까지
                raise SqlSyntaxError(
                    f'trailing junk after numeric literal at or near "{text[pos:junk_end]}"', position=pos + 1
                )
            yield Token(T.INTEGER, int(m.group()), pos, end, line, pos - line_start + 1)
            pos = end
            continue

        if kind == "string":
            raw = m.group()
            if len(raw) < 2 or raw[-1] != "'" or _odd_trailing_quotes(raw):
                raise SqlSyntaxError("unterminated quoted string", position=pos + 1)
            yield Token(T.STRING, raw[1:-1].replace("''", "'"), pos, end, line, pos - line_start + 1)
            nl = raw.count("\n")
            if nl:  # 문자열 안의 줄바꿈도 줄 번호에 반영
                line += nl
                line_start = pos + raw.rfind("\n") + 1
            pos = end
            continue

        if kind == "qident":
            raw = m.group()
            if len(raw) < 2 or raw[-1] != '"' or _odd_trailing_quotes(raw):
                raise SqlSyntaxError("unterminated quoted identifier", position=pos + 1)
            name = raw[1:-1].replace('""', '"')
            if not name:
                raise SqlSyntaxError("zero-length delimited identifier", position=pos + 1)
            _check_ident_length(name, pos)
            yield Token(T.IDENT, name, pos, end, line, pos - line_start + 1, True)
            nl = raw.count("\n")
            if nl:
                line += nl
                line_start = pos + raw.rfind("\n") + 1
            pos = end
            continue

        if kind == "line_comment":
            pos = end  # 줄 끝의 '\n' 은 다음 ws 가 처리한다
            continue

        if kind == "block_comment":
            # PostgreSQL 처럼 중첩을 지원한다: '/*' 를 만나면 깊이 +1, '*/' 를 만나면 -1, 0 이 되면 끝.
            # 정규식은 괄호 짝 맞추기(중첩)를 표현할 수 없으므로 구분자만 정규식으로 찾고 깊이는 직접 센다.
            depth = 1
            scan = end
            while depth:
                d = _COMMENT_DELIM_RE.search(text, scan)
                if d is None:
                    raise SqlSyntaxError("unterminated /* comment", position=pos + 1)
                depth += 1 if d.group() == "/*" else -1
                scan = d.end()
            nl = text.count("\n", pos, scan)
            if nl:
                line += nl
                line_start = text.rfind("\n", pos, scan) + 1
            pos = scan
            continue

        if kind == "numeric":
            raise FeatureNotSupported("numeric literals are not supported", position=pos + 1)
        if kind == "prefixed_string":
            prefix = m.group()[:-1]
            raise FeatureNotSupported(
                f"{prefix.upper()}'...' string literals are not supported", position=pos + 1
            )
        if kind == "unicode_ident":
            raise FeatureNotSupported('U&"..." identifiers are not supported', position=pos + 1)
        if kind == "param":
            raise FeatureNotSupported(f"parameters ({m.group()}) are not supported", position=pos + 1)
        if kind == "dollar_quote":
            raise FeatureNotSupported("dollar-quoted strings are not supported", position=pos + 1)
        # other: 알 수 없는 문자
        raise SqlSyntaxError(f'syntax error at or near "{m.group()}"', position=pos + 1)

    yield Token(T.EOF, None, n, n, line, n - line_start + 1)


def _odd_trailing_quotes(raw: str) -> bool:
    """닫는 따옴표처럼 보이는 끝이 사실 이스케이프('' 또는 "")의 일부인지 확인한다.

    정규식 ``'[^']*(?:''[^']*)*'?`` 는 닫히지 않은 ``'abc''`` 도 끝의 ``'`` 를 닫는 따옴표로 매치할 수 있다.
    여는 따옴표를 뺀 나머지 끝부분의 연속 따옴표 수가 짝수면 전부 이스케이프 → 닫히지 않은 것.
    """
    q = raw[0]
    body = raw[1:]
    k = len(body) - len(body.rstrip(q))
    return k % 2 == 0


def tokenize(text: str) -> list[Token]:
    """``text`` 전체를 토큰 리스트로 만든다 (마지막은 EOF). 첫 오류에서 예외."""
    return list(iter_tokens(text))


@dataclass(frozen=True, slots=True)
class StatementSource:
    """원문에서 잘라낸 SQL 한 문장.

    Attributes:
        text: 문장 원문. 첫 토큰부터 마지막 토큰까지(앞뒤 공백/주석과 세미콜론 제외).
            ``error`` 가 있으면 오류가 난 문장의 시작부터 원문 끝까지.
        start_offset: ``text`` 가 원문에서 시작하는 위치 (0부터).
        error: 렉싱 오류. split_statements 결과의 **마지막 원소에만** 있을 수 있다.
            이 원소는 실행할 수 없고, 오류 이후의 원문은 신뢰할 수 없어 더 분리하지 않는다.
        tokens: 분리할 때 만든 토큰 (원문 기준 offset, EOF 제외). 파서가 다시 렉싱하지 않도록 재사용한다.
    """

    text: str
    start_offset: int
    error: DatabaseError | None = None
    tokens: tuple[Token, ...] | None = field(default=None, repr=False, compare=False)


def iter_statements(text: str) -> Iterator[StatementSource]:
    """:func:`split_statements` 의 스트리밍 버전. 문장을 하나씩 내보낸다.

    큰 스크립트에서 모든 문장의 토큰을 한꺼번에 메모리에 들고 있지 않아도 되므로,
    파싱·실행을 문장 단위로 이어 붙이면 메모리와 GC 부담이 줄어든다 (parse_script 가 이 방식을 쓴다).
    """
    cur: list[Token] = []  # 현재 문장의 토큰
    SEMI, EOF = TokenType.SEMICOLON, TokenType.EOF
    append = cur.append
    try:
        # try 를 루프 밖에 두어 토큰마다 예외 처리 비용을 내지 않는다.
        for tok in iter_tokens(text):
            tt = tok.type
            if tt is SEMI or tt is EOF:
                # 세미콜론 "토큰"에서만 자른다. 문자열/주석 안의 ';' 는 애초에 토큰이 아니다.
                if cur:
                    start, end = cur[0].offset, cur[-1].end
                    yield StatementSource(text[start:end], start, None, tuple(cur))
                    cur.clear()
            else:
                append(tok)
    except DatabaseError as exc:
        # 오류 문장의 시작: 이미 토큰이 있으면 그 첫 토큰, 없으면 오류 위치.
        # 오류 이후는 렉서가 해석할 수 없으므로 원문 끝까지를 한 덩어리로 담고 멈춘다.
        start = cur[0].offset if cur else (exc.position - 1 if exc.position else len(text))
        yield StatementSource(text[start:], start, exc)


def split_statements(text: str) -> list[StatementSource]:
    """``text`` 를 세미콜론 **토큰** 기준으로 문장 단위로 나눈다.

    문자열/인용 식별자/주석 안의 세미콜론은 토큰이 아니므로 자르지 않는다.
    공백·주석만 있는 문장(``;;`` 포함)은 제외한다.

    렉싱 오류가 나면 그 전까지의 완전한 문장들을 반환하고, 마지막 원소로
    ``StatementSource(text=<오류 문장 시작부터 끝까지>, start_offset=..., error=<예외>)`` 를 붙인다.
    렉서는 오류 지점 이후를 어떻게 해석해야 할지 모르므로(예: 닫히지 않은 따옴표 뒤는 전부 문자열일 수 있음)
    그 뒤로는 분리하지 않는다. 구문 오류(파서 오류)는 여기서 검사하지 않으므로 다른 문장에 영향이 없다.
    """
    return list(iter_statements(text))
