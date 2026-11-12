"""스키마 카탈로그: ``<db_dir>/schema/<table>.json`` 로딩과 검증.

디렉터리 규칙::

    <db_dir>/schema/<table>.json   # 스키마 정의 (이 모듈이 읽음)
    <db_dir>/data/<table>.tbl      # 테이블 데이터 파일 (storage.TableFile 이 사용)

이름 규칙은 PostgreSQL 의 비인용 식별자를 따른다: 테이블/컬럼 이름은 소문자로,
타입 이름은 대문자로 정규화한다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import InvalidSchemaDefinition, UndefinedTable

__all__ = ["BIGINT", "TEXT", "SUPPORTED_TYPES", "Column", "TableSchema", "Catalog", "parse_schema"]

BIGINT = "BIGINT"
TEXT = "TEXT"
#: 지원 타입 (불변 상수이므로 모듈 수준에 두어도 스레드 안전)
SUPPORTED_TYPES: frozenset[str] = frozenset({BIGINT, TEXT})

# 비인용 식별자: 문자/밑줄로 시작, 이후 문자/숫자/밑줄. PostgreSQL 기본 최대 63바이트.
_IDENT_RE = re.compile(r"[a-z_][a-z0-9_]*\Z")
_MAX_IDENT_BYTES = 63
_COLUMN_KEYS = frozenset({"name", "type", "nullable", "auto_increment"})
_TABLE_KEYS = frozenset({"table", "columns"})


@dataclass(frozen=True, slots=True)
class Column:
    """테이블 컬럼 정의.

    Attributes:
        name: 소문자로 정규화된 컬럼 이름.
        type: ``"BIGINT"`` 또는 ``"TEXT"``.
        nullable: NULL 허용 여부.
        auto_increment: 자동 증가 여부 (``id`` 에만 True).
    """

    name: str
    type: str
    nullable: bool = True
    auto_increment: bool = False


@dataclass(frozen=True, slots=True)
class TableSchema:
    """테이블 스키마. 첫 컬럼은 항상 ``id BIGINT`` auto_increment.

    Attributes:
        name: 소문자로 정규화된 테이블 이름.
        columns: 전체 컬럼 (``id`` 포함) 튜플.
    """

    name: str
    columns: tuple[Column, ...]
    # data_columns 는 자주 쓰이므로 생성 시 한 번 계산해 둔다 (비교/repr 에서 제외).
    _data_columns: tuple[Column, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_data_columns", tuple(self.columns[1:]))

    @property
    def data_columns(self) -> tuple[Column, ...]:
        """``id`` 를 제외한 컬럼 (사용자가 값을 넣는 컬럼) 목록."""
        return self._data_columns

    @property
    def column_names(self) -> tuple[str, ...]:
        """전체 컬럼 이름 (``id`` 포함)."""
        return tuple(c.name for c in self.columns)

    @classmethod
    def from_dict(cls, data: Any) -> TableSchema:
        """JSON 에서 읽은 dict 를 검증해 :class:`TableSchema` 로 만든다."""
        return parse_schema(data)


def _fail(msg: str) -> InvalidSchemaDefinition:
    return InvalidSchemaDefinition(msg)


def _normalize_ident(raw: Any, what: str) -> str:
    """식별자를 소문자로 정규화하고 형식을 검사한다."""
    if not isinstance(raw, str) or not raw:
        raise _fail(f"{what} must be a non-empty string")
    name = raw.lower()
    if not _IDENT_RE.match(name):
        raise _fail(f"invalid {what} \"{raw}\"")
    if len(name.encode("utf-8")) > _MAX_IDENT_BYTES:
        raise _fail(f"{what} \"{raw}\" is too long (max {_MAX_IDENT_BYTES} bytes)")
    return name


def _get_bool(spec: dict[str, Any], key: str, default: bool, col: str) -> bool:
    value = spec.get(key, default)
    if not isinstance(value, bool):
        raise _fail(f"\"{key}\" of column \"{col}\" must be a boolean")
    return value


def _parse_column(spec: Any, index: int) -> Column:
    if not isinstance(spec, dict):
        raise _fail(f"column #{index + 1} must be an object")
    unknown = set(spec) - _COLUMN_KEYS
    if unknown:
        raise _fail(f"unknown key(s) in column #{index + 1}: {sorted(unknown)}")
    name = _normalize_ident(spec.get("name"), "column name")
    raw_type = spec.get("type")
    if not isinstance(raw_type, str):
        raise _fail(f"type of column \"{name}\" must be a string")
    col_type = raw_type.upper()
    if col_type not in SUPPORTED_TYPES:
        raise _fail(f"type \"{raw_type}\" of column \"{name}\" is not supported")
    auto_inc = _get_bool(spec, "auto_increment", False, name)
    if index == 0:
        # id 는 항상 NOT NULL. nullable: true 를 명시하면 모순이므로 거부한다.
        if spec.get("nullable", False) is not False:
            raise _fail("column \"id\" cannot be nullable")
        nullable = False
    else:
        nullable = _get_bool(spec, "nullable", True, name)
    return Column(name=name, type=col_type, nullable=nullable, auto_increment=auto_inc)


def parse_schema(data: Any, *, expected_name: str | None = None) -> TableSchema:
    """스키마 dict 를 검증하고 정규화한다.

    Args:
        data: ``json.load`` 결과.
        expected_name: 주어지면 ``table`` 값이 이 이름(정규화 후)과 같아야 한다.

    Raises:
        InvalidSchemaDefinition: 규칙 위반 시.
    """
    if not isinstance(data, dict):
        raise _fail("schema must be a JSON object")
    unknown = set(data) - _TABLE_KEYS
    if unknown:
        raise _fail(f"unknown key(s) in schema: {sorted(unknown)}")
    table = _normalize_ident(data.get("table"), "table name")
    if expected_name is not None and table != expected_name.lower():
        raise _fail(f"table name \"{table}\" does not match schema file name \"{expected_name}\"")

    raw_cols = data.get("columns")
    if not isinstance(raw_cols, list):
        raise _fail("\"columns\" must be a list")
    if len(raw_cols) < 2:
        raise _fail("table must have at least 2 columns (id + 1 data column)")

    columns = tuple(_parse_column(spec, i) for i, spec in enumerate(raw_cols))

    first = columns[0]
    if first.name != "id" or first.type != BIGINT or not first.auto_increment:
        raise _fail("first column must be \"id BIGINT\" with auto_increment true")
    for col in columns[1:]:
        if col.auto_increment:
            raise _fail(f"auto_increment is only allowed on column \"id\" (found on \"{col.name}\")")
    seen: set[str] = set()
    for col in columns:
        if col.name in seen:
            raise _fail(f"column \"{col.name}\" specified more than once")
        seen.add(col.name)

    return TableSchema(name=table, columns=columns)


class Catalog:
    """``<db_dir>/schema`` 아래의 스키마를 읽고 인스턴스 내부에 캐시한다.

    모듈 전역 캐시를 쓰지 않으므로 Catalog 인스턴스마다 독립적이다.
    이번 단계에서는 스레드 안전하지 않다 (Phase 3 에서 ``_cache`` 접근을 락으로 보호).
    """

    def __init__(self, db_dir: str | Path) -> None:
        """``db_dir`` 을 루트로 하는 카탈로그를 만든다."""
        self.db_dir: Path = Path(db_dir)
        self._cache: dict[str, TableSchema] = {}

    @property
    def schema_dir(self) -> Path:
        """스키마 JSON 디렉터리."""
        return self.db_dir / "schema"

    @property
    def data_dir(self) -> Path:
        """테이블 데이터 파일 디렉터리."""
        return self.db_dir / "data"

    def schema_path(self, table_name: str) -> Path:
        """테이블의 스키마 JSON 경로."""
        return self.schema_dir / f"{table_name.lower()}.json"

    def data_path(self, table_name: str) -> Path:
        """테이블의 데이터 파일 경로 (``<db_dir>/data/<table>.tbl``)."""
        return self.data_dir / f"{table_name.lower()}.tbl"

    def get(self, table_name: str) -> TableSchema:
        """테이블 스키마를 반환한다.

        Raises:
            UndefinedTable: 스키마 파일이 없을 때.
            InvalidSchemaDefinition: JSON 이 깨졌거나 규칙을 위반할 때.
        """
        key = table_name.lower()
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        # 경로 조작 방지: 식별자 형식이 아니면 존재할 수 없는 테이블로 본다.
        if not _IDENT_RE.match(key):
            raise UndefinedTable(f"relation \"{table_name}\" does not exist")
        path = self.schema_path(key)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise UndefinedTable(f"relation \"{key}\" does not exist") from None
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise InvalidSchemaDefinition(f"invalid JSON in {path.name}: {exc}") from exc
        schema = parse_schema(data, expected_name=key)
        # Phase 3: 이 캐시 갱신은 락으로 보호할 지점.
        self._cache[key] = schema
        return schema
