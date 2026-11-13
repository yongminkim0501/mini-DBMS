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
    "InvalidSchemaName",
    "UndefinedColumn",
    "DuplicateColumn",
    "GeneratedAlways",
    "UndefinedFunction",
    "InvalidTextRepresentation",
    "UniqueViolation",
    "UndefinedObject",
    "LockNotAvailable",
    "TooManyConnections",
    "InsufficientResources",
    "QueryCanceled",
    "ProtocolViolation",
    "UndefinedParameter",
    "IndeterminateDatatype",
    "DuplicatePreparedStatement",
    "DuplicateCursor",
    "InvalidSqlStatementName",
    "InvalidCursorName",
    "InvalidAuthorizationSpecification",
    "InvalidCatalogName",
    "InvalidBinaryRepresentation",
    "AmbiguousParameter",
    "CantChangeRuntimeParam",
    "AdminShutdown",
    "SEVERITIES",
]

#: ErrorResponse 의 S/V 필드에 쓸 수 있는 심각도. FATAL 은 오류를 보낸 뒤 연결을 끊는다.
SEVERITIES: frozenset[str] = frozenset({"ERROR", "FATAL"})


class DatabaseError(Exception):
    """minidb 의 모든 데이터베이스 오류의 기본 클래스.

    Attributes:
        sqlstate: 5자리 PostgreSQL SQLSTATE 코드.
        message: 사람이 읽을 수 있는 오류 메시지.
        position: SQL 원문 기준 1부터 시작하는 문자 위치 (PostgreSQL ErrorResponse 의 P 필드).
            SQL 텍스트와 관계없는 오류(저장 계층 등)는 None.
        detail: 부가 설명 (ErrorResponse 의 D 필드). psql 은 "DETAIL:  ..." 로 출력한다.
        hint: 해결 힌트 (ErrorResponse 의 H 필드). psql 은 "HINT:  ..." 로 출력한다.
        severity: ``ERROR`` 또는 ``FATAL`` (ErrorResponse 의 S/V 필드).
    """

    #: 하위 클래스가 덮어쓰는 기본 SQLSTATE (XX000 = internal_error)
    default_sqlstate: str = "XX000"

    def __init__(
        self,
        message: str,
        *,
        sqlstate: str | None = None,
        position: int | None = None,
        detail: str | None = None,
        hint: str | None = None,
        severity: str | None = None,
    ) -> None:
        """메시지와 (선택적으로) SQLSTATE, SQL 원문 위치, DETAIL, HINT, 심각도를 지정해 예외를 만든다."""
        super().__init__(message)
        self.message: str = message
        self.sqlstate: str = sqlstate if sqlstate is not None else self.default_sqlstate
        self.position: int | None = position
        self.detail: str | None = detail
        self.hint: str | None = hint
        sev = severity if severity is not None else self.default_severity
        if sev not in SEVERITIES:
            raise ValueError(f"invalid severity {sev!r}")
        #: ``ERROR`` (문장만 실패) 또는 ``FATAL`` (연결 종료). wire protocol 의 S/V 필드.
        self.severity: str = sev

    #: 하위 클래스가 덮어쓰는 기본 심각도
    default_severity: str = "ERROR"

    def __str__(self) -> str:
        return f"{self.severity}:  {self.message} (SQLSTATE {self.sqlstate})"


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


class InvalidSchemaName(DatabaseError):
    """존재하지 않는 스키마 (3F000)."""

    default_sqlstate = "3F000"


class UndefinedColumn(DatabaseError):
    """존재하지 않는 컬럼 (42703)."""

    default_sqlstate = "42703"


class DuplicateColumn(DatabaseError):
    """INSERT 컬럼 목록에 같은 컬럼이 두 번 (42701)."""

    default_sqlstate = "42701"


class GeneratedAlways(DatabaseError):
    """GENERATED ALWAYS 컬럼(id)에 DEFAULT 가 아닌 값 (428C9)."""

    default_sqlstate = "428C9"


class UndefinedFunction(DatabaseError):
    """함수/연산자가 없음 (42883). PostgreSQL 은 "operator does not exist" 도 이 코드다."""

    default_sqlstate = "42883"


class InvalidTextRepresentation(DatabaseError):
    """문자열을 해당 타입으로 해석할 수 없음 (22P02)."""

    default_sqlstate = "22P02"


class UniqueViolation(DatabaseError):
    """유일 제약 위반 (23505). 예: ``duplicate key value violates unique constraint "users_pkey"``."""

    default_sqlstate = "23505"


class UndefinedObject(DatabaseError):
    """존재하지 않는 객체 (42704). 예: ``unrecognized configuration parameter "x"``."""

    default_sqlstate = "42704"


class LockNotAvailable(DatabaseError):
    """락 대기 시간 초과 (55P03). 예: ``could not obtain lock on relation "users"``."""

    default_sqlstate = "55P03"


class TooManyConnections(DatabaseError):
    """최대 연결 수 초과 (53300). PostgreSQL: ``sorry, too many clients already``."""

    default_sqlstate = "53300"


class InsufficientResources(DatabaseError):
    """서버 자원 부족 (53000). 작업 큐가 가득 찼을 때."""

    default_sqlstate = "53000"


class QueryCanceled(DatabaseError):
    """문장 취소 (57014). 서버 종료 등으로 처리하지 못한 요청."""

    default_sqlstate = "57014"


class ProtocolViolation(DatabaseError):
    """프로토콜 위반 (08P01). 예: Bind 의 파라미터 개수 불일치, 잘못된 메시지 길이."""

    default_sqlstate = "08P01"


class UndefinedParameter(DatabaseError):
    """존재하지 않는 파라미터 (42P02). 예: 단순 쿼리의 ``$1``: ``there is no parameter $1``."""

    default_sqlstate = "42P02"


class IndeterminateDatatype(DatabaseError):
    """파라미터 타입을 정할 수 없음 (42P18). 예: ``could not determine data type of parameter $1``."""

    default_sqlstate = "42P18"


class DuplicatePreparedStatement(DatabaseError):
    """같은 이름의 준비된 문장이 이미 있음 (42P05)."""

    default_sqlstate = "42P05"


class DuplicateCursor(DatabaseError):
    """같은 이름의 포털(커서)이 이미 있음 (42P03)."""

    default_sqlstate = "42P03"


class InvalidSqlStatementName(DatabaseError):
    """존재하지 않는 준비된 문장 (26000)."""

    default_sqlstate = "26000"


class InvalidCursorName(DatabaseError):
    """존재하지 않는 포털 (34000)."""

    default_sqlstate = "34000"


class InvalidAuthorizationSpecification(DatabaseError):
    """인증 정보 오류 (28000). 예: 시작 패킷에 user 가 없음."""

    default_sqlstate = "28000"


class InvalidCatalogName(DatabaseError):
    """존재하지 않는 데이터베이스 (3D000)."""

    default_sqlstate = "3D000"


class InvalidBinaryRepresentation(DatabaseError):
    """바이너리 형식 값의 길이/내용이 잘못됨 (22P03)."""

    default_sqlstate = "22P03"


class AmbiguousParameter(DatabaseError):
    """같은 파라미터가 서로 다른 타입으로 추론됨 (42P08). 예: ``inconsistent types deduced for parameter $1``."""

    default_sqlstate = "42P08"


class CantChangeRuntimeParam(DatabaseError):
    """읽기 전용 설정을 바꾸려 함 (55P02). 예: ``parameter "server_version" cannot be changed``."""

    default_sqlstate = "55P02"


class AdminShutdown(DatabaseError):
    """서버 종료로 연결을 끊음 (57P01). 항상 FATAL."""

    default_sqlstate = "57P01"
    default_severity = "FATAL"
