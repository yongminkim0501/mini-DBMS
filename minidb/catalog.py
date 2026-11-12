"""스키마 카탈로그: ``<db_dir>/schema/*.json`` 로딩과 검증.

디렉터리 규칙::

    <db_dir>/schema/<아무 이름>.json   # 스키마 정의. 관례적으로 <oid>.json. 파일 이름은 의미가 없다
    <db_dir>/data/<oid>.tbl            # 테이블 데이터 파일 (storage.TableFile 이 사용)

**이름과 파일의 분리 (PostgreSQL 의 relfilenode 방식)**: 테이블 이름은 카탈로그(JSON 내용)에만 있고,
파일은 숫자 ``oid`` 로 이름 붙인다. 파일 시스템이 이름 비교에 관여하지 않으므로

- 대소문자를 구분하지 않는 파일 시스템(macOS APFS 기본값, Windows)에서도 ``"Users"`` 와 ``users`` 가 서로 다른
  테이블·파일로 공존하고,
- 한글 이름이 파일 시스템에 따라 NFC/NFD 로 바뀌어 저장되는 문제와 무관하다.

**이름은 정규화하지 않는다**: JSON 의 테이블/컬럼 이름은 적힌 그대로가 이름이다 (PostgreSQL 카탈로그도 접힌 뒤의
이름을 그대로 저장한다). 비인용 식별자의 소문자 변환은 SQL 렉서만 책임진다. 그래서 SQL 에서 ``FROM Users`` 로
찾는 테이블은 JSON 에 ``"users"`` 로, ``FROM "Users"`` 로 찾는 테이블은 ``"Users"`` 로 적는다.
타입 이름(BIGINT/TEXT)은 식별자가 아니므로 대소문자를 무시하고 대문자로 정규화한다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import InvalidSchemaDefinition, UndefinedTable
from .identifiers import MAX_IDENTIFIER_BYTES, identifier_too_long

__all__ = ["BIGINT", "TEXT", "SUPPORTED_TYPES", "MAX_OID", "Column", "TableSchema", "Catalog", "parse_schema"]

BIGINT = "BIGINT"
TEXT = "TEXT"
#: 지원 타입 (불변 상수이므로 모듈 수준에 두어도 스레드 안전)
SUPPORTED_TYPES: frozenset[str] = frozenset({BIGINT, TEXT})
#: oid 최댓값 (PostgreSQL 의 oid 는 부호 없는 32비트)
MAX_OID = 2**32 - 1

_COLUMN_KEYS = frozenset({"name", "type", "nullable", "auto_increment"})
_TABLE_KEYS = frozenset({"oid", "table", "columns"})


@dataclass(frozen=True, slots=True)
class Column:
    """테이블 컬럼 정의.

    Attributes:
        name: 컬럼 이름 (JSON 에 적힌 그대로).
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
        name: 테이블 이름 (JSON 에 적힌 그대로).
        columns: 전체 컬럼 (``id`` 포함) 튜플.
        oid: 테이블 번호. 데이터 파일 이름(``<oid>.tbl``)에 쓰인다.
    """

    name: str
    columns: tuple[Column, ...]
    oid: int
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


def _check_name(raw: Any, what: str) -> str:
    """카탈로그 이름 검증: 비어 있지 않은 문자열, UTF-8 63바이트 이하, NUL 없음. 정규화는 하지 않는다."""
    if not isinstance(raw, str) or not raw:
        raise _fail(f"{what} must be a non-empty string")
    if "\x00" in raw:
        raise _fail(f"{what} {raw!r} contains a NUL character")
    if identifier_too_long(raw):
        raise _fail(f"{what} \"{raw}\" is too long (max {MAX_IDENTIFIER_BYTES} bytes)")
    return raw


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
    name = _check_name(spec.get("name"), "column name")
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


def parse_schema(data: Any) -> TableSchema:
    """스키마 dict 를 검증한다 (이름은 적힌 그대로 쓴다).

    Args:
        data: ``json.load`` 결과. ``oid``(양의 정수), ``table``, ``columns`` 가 필수.

    Raises:
        InvalidSchemaDefinition: 규칙 위반 시.
    """
    if not isinstance(data, dict):
        raise _fail("schema must be a JSON object")
    unknown = set(data) - _TABLE_KEYS
    if unknown:
        raise _fail(f"unknown key(s) in schema: {sorted(unknown)}")

    oid = data.get("oid")
    # bool 은 int 의 하위 클래스지만 oid 로는 거부한다.
    if type(oid) is not int or not 1 <= oid <= MAX_OID:
        raise _fail(f"\"oid\" must be an integer between 1 and {MAX_OID}, got {oid!r}")

    table = _check_name(data.get("table"), "table name")

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
        # 이름은 정확히 비교한다: "a" 와 "A" 는 다른 컬럼 (PostgreSQL 의 인용 식별자와 같음).
        if col.name in seen:
            raise _fail(f"column \"{col.name}\" specified more than once")
        seen.add(col.name)

    return TableSchema(name=table, columns=columns, oid=oid)


class Catalog:
    """``<db_dir>/schema/*.json`` 을 모두 읽어 **JSON 안의 테이블 이름**을 키로 하는 사전을 만든다.

    - 스키마 파일 이름은 의미가 없다 (관례적으로 ``<oid>.json``). 이름으로 파일 시스템을 조회하지 않는다.
    - 처음 조회할 때 한 번 읽어 인스턴스 안에 캐시한다 (모듈 전역 캐시 없음). 파일을 추가했으면 :meth:`reload`.
    - 테이블 이름 중복, oid 중복은 InvalidSchemaDefinition.

    이번 단계에서는 스레드 안전하지 않다 (Phase 3 에서 ``_tables`` 적재를 락으로 보호).
    """

    def __init__(self, db_dir: str | Path) -> None:
        """``db_dir`` 을 루트로 하는 카탈로그를 만든다. 파일은 첫 조회 때 읽는다."""
        self.db_dir: Path = Path(db_dir)
        self._tables: dict[str, TableSchema] | None = None
        self._sources: dict[str, Path] = {}

    @property
    def schema_dir(self) -> Path:
        """스키마 JSON 디렉터리."""
        return self.db_dir / "schema"

    @property
    def data_dir(self) -> Path:
        """테이블 데이터 파일 디렉터리."""
        return self.db_dir / "data"

    def reload(self) -> None:
        """스키마 디렉터리를 다시 읽는다."""
        self._tables = None
        self._load()

    def _load(self) -> dict[str, TableSchema]:
        # [LOCK] Phase 3: 최초 적재(_tables/_sources 갱신)는 락으로 보호할 지점.
        if self._tables is not None:
            return self._tables
        tables: dict[str, TableSchema] = {}
        sources: dict[str, Path] = {}
        by_oid: dict[int, Path] = {}
        paths = sorted(self.schema_dir.glob("*.json")) if self.schema_dir.is_dir() else []
        for path in paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise InvalidSchemaDefinition(f"invalid JSON in {path.name}: {exc}") from exc
            try:
                schema = parse_schema(data)
            except InvalidSchemaDefinition as exc:
                raise InvalidSchemaDefinition(f"{path.name}: {exc.message}") from None
            if schema.name in tables:
                raise InvalidSchemaDefinition(
                    f"table \"{schema.name}\" is defined more than once ({sources[schema.name].name}, {path.name})"
                )
            if schema.oid in by_oid:
                raise InvalidSchemaDefinition(
                    f"oid {schema.oid} is used more than once ({by_oid[schema.oid].name}, {path.name})"
                )
            tables[schema.name] = schema
            sources[schema.name] = path
            by_oid[schema.oid] = path
        self._tables, self._sources = tables, sources
        return tables

    def get(self, table_name: str) -> TableSchema:
        """테이블 이름으로 스키마를 반환한다 (**정확 일치**).

        ``table_name`` 은 SQL 렉서가 만든 식별자 값을 그대로 넘긴다: 비인용 ``Users`` 는 렉서가 이미
        ``users`` 로 접었고, 인용 ``"Users"`` 는 ``Users`` 다. 여기서는 다시 접지 않으므로 PostgreSQL 과 같이
        ``"Users"`` 와 ``users`` 는 다른 테이블이다.

        Raises:
            UndefinedTable: 그런 이름의 테이블이 없을 때.
            InvalidSchemaDefinition: 스키마 파일이 잘못되었거나 이름/oid 가 중복될 때.
        """
        schema = self._load().get(table_name)
        if schema is None:
            raise UndefinedTable(f"relation \"{table_name}\" does not exist")
        return schema

    def tables(self) -> list[TableSchema]:
        """모든 테이블 스키마 (oid 순)."""
        return sorted(self._load().values(), key=lambda s: s.oid)

    def schema_file(self, table_name: str) -> Path:
        """테이블 정의를 읽어 온 JSON 파일 경로 (진단용)."""
        self.get(table_name)
        return self._sources[table_name]

    def data_path(self, table_name: str) -> Path:
        """테이블의 데이터 파일 경로 ``<db_dir>/data/<oid>.tbl``."""
        return self.data_dir / f"{self.get(table_name).oid}.tbl"
