"""PostgreSQL SQLSTATE 코드를 가진 minidb 예외 체계.

모든 예외는 :class:`DatabaseError` 를 상속하며 ``sqlstate`` 와 ``message`` 를 가진다.
``str(err)`` 는 psql 출력과 비슷한 ``"ERROR:  {message} (SQLSTATE {sqlstate})"`` 형식이다.
Phase 3 에서 PostgreSQL wire protocol 의 ErrorResponse(C 필드)에 그대로 쓸 수 있도록
코드는 클래스 속성으로 고정한다.
"""

from __future__ import annotations

__all__ = [
    "DatabaseError",
    "DatatypeMismatch",
    "NumericValueOutOfRange",
    "NotNullViolation",
    "ProgramLimitExceeded",
    "DataCorrupted",
    "UndefinedTable",
    "InvalidSchemaDefinition",
    "InvalidParameterValue",
    "SequenceGeneratorLimitExceeded",
    "CharacterNotInRepertoire",
    "SqlSyntaxError",
    "FeatureNotSupported",
    "NameTooLong",
    "InvalidRowCountInLimitClause",
]


class DatabaseError(Exception):
    """minidb 의 모든 데이터베이스 오류의 기본 클래스.

    Attributes:
        sqlstate: 5자리 PostgreSQL SQLSTATE 코드.
        message: 사람이 읽을 수 있는 오류 메시지.
        position: SQL 원문 기준 1부터 시작하는 문자 위치 (PostgreSQL ErrorResponse 의 P 필드).
            SQL 텍스트와 관계없는 오류(저장 계층 등)는 None.
    """

    #: 하위 클래스가 덮어쓰는 기본 SQLSTATE (XX000 = internal_error)
    default_sqlstate: str = "XX000"

    def __init__(self, message: str, *, sqlstate: str | None = None, position: int | None = None) -> None:
        """메시지와 (선택적으로) SQLSTATE, SQL 원문 위치를 지정해 예외를 만든다."""
        super().__init__(message)
        self.message: str = message
        self.sqlstate: str = sqlstate if sqlstate is not None else self.default_sqlstate
        self.position: int | None = position

    def __str__(self) -> str:
        return f"ERROR:  {self.message} (SQLSTATE {self.sqlstate})"


class DatatypeMismatch(DatabaseError):
    """값의 파이썬 타입이 컬럼 타입과 다름 (42804)."""

    default_sqlstate = "42804"


class NumericValueOutOfRange(DatabaseError):
    """BIGINT 범위 초과 (22003)."""

    default_sqlstate = "22003"


class NotNullViolation(DatabaseError):
    """NOT NULL 컬럼에 NULL 삽입 (23502)."""

    default_sqlstate = "23502"


class ProgramLimitExceeded(DatabaseError):
    """TEXT 최대 길이 등 구현 한계 초과 (54000)."""

    default_sqlstate = "54000"


class DataCorrupted(DatabaseError):
    """파일 손상, magic 불일치 등 (XX001)."""

    default_sqlstate = "XX001"


class UndefinedTable(DatabaseError):
    """존재하지 않는 테이블 (42P01)."""

    default_sqlstate = "42P01"


class InvalidSchemaDefinition(DatabaseError):
    """스키마 JSON 자체가 잘못됨 (42P16)."""

    default_sqlstate = "42P16"


class InvalidParameterValue(DatabaseError):
    """값 개수 불일치 등 잘못된 인자 (22023)."""

    default_sqlstate = "22023"


class SequenceGeneratorLimitExceeded(DatabaseError):
    """id 시퀀스가 BIGINT 최댓값에 도달해 더 부여할 수 없음 (2200H)."""

    default_sqlstate = "2200H"


class CharacterNotInRepertoire(DatabaseError):
    """UTF-8 로 인코딩할 수 없는 문자열 (짝 없는 서로게이트 등) (22021)."""

    default_sqlstate = "22021"


class SqlSyntaxError(DatabaseError):
    """SQL 구문 오류 (42601). 파이썬 내장 SyntaxError 와 이름이 겹치지 않게 Sql 접두사를 붙였다."""

    default_sqlstate = "42601"


class FeatureNotSupported(DatabaseError):
    """PostgreSQL 에는 있지만 minidb 가 지원하지 않는 기능 (0A000)."""

    default_sqlstate = "0A000"


class NameTooLong(DatabaseError):
    """식별자가 63바이트(UTF-8)를 넘음 (42622)."""

    default_sqlstate = "42622"


class InvalidRowCountInLimitClause(DatabaseError):
    """LIMIT 에 음수 (2201W)."""

    default_sqlstate = "2201W"
