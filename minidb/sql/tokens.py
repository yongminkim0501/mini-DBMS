"""SQL 토큰 정의."""

from __future__ import annotations

from enum import Enum
from typing import Any, NamedTuple

__all__ = ["TokenType", "Token", "RESERVED_KEYWORDS"]


class TokenType(Enum):
    """토큰 종류."""

    KEYWORD = "KEYWORD"  # 예약 키워드 (value 는 대문자)
    IDENT = "IDENT"  # 식별자 (value 는 정규화된 이름: 비인용은 소문자, 인용은 원문 그대로)
    STRING = "STRING"  # '...' 문자열 (value 는 이스케이프를 푼 str)
    INTEGER = "INTEGER"  # 정수 (value 는 int)
    LPAREN = "("
    RPAREN = ")"
    COMMA = ","
    SEMICOLON = ";"
    STAR = "*"
    DOT = "."
    EQ = "="
    NE = "<>"  # '<>' 와 '!=' 둘 다 (원문은 Token 의 offset/end 로 확인)
    LT = "<"
    LE = "<="
    GT = ">"
    GE = ">="
    MINUS = "-"
    EOF = "EOF"


#: 예약 키워드. 인용 부호 없이 식별자 자리에 오면 구문 오류.
RESERVED_KEYWORDS: frozenset[str] = frozenset(
    {"SELECT", "FROM", "WHERE", "INSERT", "INTO", "VALUES", "RETURNING", "LIMIT", "ALL", "AND", "NULL", "DEFAULT"}
)


class Token(NamedTuple):
    """토큰 하나.

    렉서가 수백만 개를 만들 수 있으므로 dataclass 대신 생성 비용이 싼 NamedTuple 을 쓴다 (불변).

    Attributes:
        type: 토큰 종류.
        value: 정규화된 값 (KEYWORD 는 대문자 str, IDENT 는 이름, STRING 은 str, INTEGER 는 int, 기호는 기호 문자열).
        offset: 원문 기준 시작 위치 (0부터).
        end: 원문 기준 끝 위치 (exclusive). ``text[offset:end]`` 가 토큰의 원문.
        line: 줄 번호 (1부터).
        column: 열 번호 (1부터, 문자 단위).
        quoted: IDENT 가 큰따옴표로 인용되었는지. 인용 식별자는 키워드처럼 해석되지 않는다.
    """

    type: TokenType
    value: Any
    offset: int
    end: int
    line: int
    column: int
    quoted: bool = False
