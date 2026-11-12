"""SQL 비인용 식별자 규칙의 단일 정의.

비인용 식별자를 접는(case folding) 곳은 **SQL 렉서 하나뿐**이다. 카탈로그(스키마 JSON)는 이름을 적힌 그대로
저장하고 정확히 비교한다 (PostgreSQL 카탈로그와 같음). 렉서, ``to_sql()`` 의 인용 판단, dump 도구의 테이블 인자처럼
"SQL 과 같은 규칙으로 이름을 해석해야 하는 곳"은 모두 이 모듈의 함수를 쓴다. 규칙을 여러 곳에 따로 두면
예를 들어 한쪽은 ``str.lower()`` 로 ``"Σ"`` 를 ``"σ"`` 로 바꾸고 다른 쪽은 그대로 두는 식으로 어긋난다.

규칙은 PostgreSQL(서버 인코딩 UTF-8)과 같다:

- 비인용 식별자의 첫 글자: ASCII 영문자, ``_``, 또는 비 ASCII 문자(U+0080 이상)
- 이후 글자: 위에 더해 ASCII 숫자와 ``$``
- 대소문자 접기(case folding): **ASCII A-Z 만** 소문자로 바꾼다. ``Ä``, ``Σ``, 한글 등은 그대로 둔다.
  PostgreSQL 은 멀티바이트 인코딩에서 로캘 의존적인 변환을 피하려고 ASCII 만 접는다.
  ``str.lower()`` 는 유니코드 전체를 변환하므로 쓰면 안 된다.
- 최대 길이: UTF-8 63바이트 (PostgreSQL 은 잘라내지만 minidb 는 오류)
"""

from __future__ import annotations

import re

__all__ = [
    "MAX_IDENTIFIER_BYTES",
    "UNQUOTED_IDENT_PATTERN",
    "fold_identifier",
    "is_valid_unquoted_identifier",
    "identifier_too_long",
]

#: 식별자 최대 길이 (UTF-8 바이트). PostgreSQL 의 NAMEDATALEN - 1.
MAX_IDENTIFIER_BYTES = 63

#: 비인용 식별자 정규식 (앵커 없음). 렉서의 토큰 정규식에 그대로 끼워 쓴다.
UNQUOTED_IDENT_PATTERN = r"[A-Za-z_\u0080-\U0010FFFF][A-Za-z0-9_$\u0080-\U0010FFFF]*"
_UNQUOTED_IDENT_RE = re.compile(UNQUOTED_IDENT_PATTERN + r"\Z")

# ASCII 'A'-'Z' → 'a'-'z' 만 매핑하는 변환표 (str.translate 는 C 로 동작해 빠르다)
_ASCII_FOLD = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}


def fold_identifier(name: str) -> str:
    """비인용 식별자를 정규화한다: ASCII A-Z 만 소문자로, 나머지 문자는 그대로.

    >>> fold_identifier("Users회원")
    'users회원'
    >>> fold_identifier("ÄΣ")
    'ÄΣ'
    """
    return name.translate(_ASCII_FOLD)


def is_valid_unquoted_identifier(name: str) -> bool:
    """``name`` 이 인용 부호 없이 쓸 수 있는 식별자 형태인가 (예약어 여부와 길이는 보지 않는다)."""
    return _UNQUOTED_IDENT_RE.match(name) is not None


def identifier_too_long(name: str) -> bool:
    """UTF-8 로 63바이트를 넘는가."""
    # 한 글자는 최대 4바이트이므로 15글자 이하는 인코딩하지 않고도 63바이트 이하임이 확실하다.
    return len(name) > MAX_IDENTIFIER_BYTES // 4 and len(name.encode("utf-8")) > MAX_IDENTIFIER_BYTES
