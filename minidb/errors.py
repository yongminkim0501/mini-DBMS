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
]


class DatabaseError(Exception):
    """minidb 의 모든 데이터베이스 오류의 기본 클래스.

    Attributes:
        sqlstate: 5자리 PostgreSQL SQLSTATE 코드.
        message: 사람이 읽을 수 있는 오류 메시지.
    """

    #: 하위 클래스가 덮어쓰는 기본 SQLSTATE (XX000 = internal_error)
    default_sqlstate: str = "XX000"

    def __init__(self, message: str, *, sqlstate: str | None = None) -> None:
        """메시지와 (선택적으로) SQLSTATE 를 지정해 예외를 만든다."""
        super().__init__(message)
        self.message: str = message
        self.sqlstate: str = sqlstate if sqlstate is not None else self.default_sqlstate

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
