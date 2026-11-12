import json

import pytest

from minidb.catalog import Catalog, Column, TableSchema, parse_schema
from minidb.errors import InvalidSchemaDefinition, UndefinedTable

USERS = {
    "table": "users",
    "columns": [
        {"name": "id", "type": "BIGINT", "auto_increment": True},
        {"name": "name", "type": "TEXT", "nullable": True},
        {"name": "age", "type": "BIGINT", "nullable": True},
    ],
}


def write_schema(tmp_path, name, data):
    d = tmp_path / "schema"
    d.mkdir(exist_ok=True)
    p = d / f"{name}.json"
    p.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")
    return p


def test_load_users(tmp_path):
    write_schema(tmp_path, "users", USERS)
    schema = Catalog(tmp_path).get("users")
    assert schema.name == "users"
    assert schema.columns[0] == Column("id", "BIGINT", nullable=False, auto_increment=True)
    assert [c.name for c in schema.data_columns] == ["name", "age"]
    assert schema.column_names == ("id", "name", "age")


def test_example_schema_in_repo_is_valid():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "db"
    assert Catalog(root).get("users").name == "users"


def test_normalization(tmp_path):
    data = {
        "table": "Users",
        "columns": [
            {"name": "ID", "type": "bigint", "auto_increment": True},
            {"name": "UserName", "type": "Text"},
        ],
    }
    write_schema(tmp_path, "users", data)
    cat = Catalog(tmp_path)
    schema = cat.get("users")  # get 은 정규화된 이름을 받는다 (SQL 렉서가 만든 식별자 값)
    assert schema.name == "users"
    assert schema.columns[0].name == "id"
    assert schema.columns[1] == Column("username", "TEXT", nullable=True, auto_increment=False)
    # 인용 식별자 "USERS" 는 users 와 다른 이름 (PostgreSQL 과 동일). get 은 다시 접지 않는다.
    with pytest.raises(UndefinedTable):
        cat.get("USERS")


def test_non_ascii_names_fold_only_ascii(tmp_path):
    data = {
        "table": "Users회원",
        "columns": [
            {"name": "id", "type": "BIGINT", "auto_increment": True},
            {"name": "이름", "type": "TEXT"},
            {"name": "ÄgeΣ", "type": "BIGINT"},
            {"name": "a$1", "type": "BIGINT"},
        ],
    }
    write_schema(tmp_path, "users회원", data)
    schema = Catalog(tmp_path).get("users회원")
    assert schema.name == "users회원"
    # Ä, Σ 는 ASCII 가 아니므로 그대로 (str.lower() 였다면 äge σ 가 됨)
    assert schema.column_names == ("id", "이름", "Äge" + "Σ", "a$1")


@pytest.mark.parametrize("bad", ["a-b", "1abc", "a b", "$x", "x.y"])
def test_invalid_identifier_forms(bad):
    with pytest.raises(InvalidSchemaDefinition):
        parse_schema(
            {
                "table": "t",
                "columns": [{"name": "id", "type": "BIGINT", "auto_increment": True}, {"name": bad, "type": "TEXT"}],
            }
        )


def test_nullable_default_true_and_false(tmp_path):
    s = parse_schema(
        {
            "table": "t",
            "columns": [
                {"name": "id", "type": "BIGINT", "auto_increment": True},
                {"name": "a", "type": "TEXT"},
                {"name": "b", "type": "BIGINT", "nullable": False},
            ],
        }
    )
    assert s.columns[0].nullable is False
    assert s.columns[1].nullable is True
    assert s.columns[2].nullable is False


def test_cache_returns_same_instance(tmp_path):
    p = write_schema(tmp_path, "users", USERS)
    cat = Catalog(tmp_path)
    a = cat.get("users")
    p.unlink()  # 캐시되어 있으므로 파일이 없어도 동작
    assert cat.get("users") is a
    # 다른 인스턴스는 캐시를 공유하지 않는다
    with pytest.raises(UndefinedTable):
        Catalog(tmp_path).get("users")


def test_undefined_table(tmp_path):
    with pytest.raises(UndefinedTable) as ei:
        Catalog(tmp_path).get("nope")
    assert ei.value.sqlstate == "42P01"


def test_undefined_table_path_traversal(tmp_path):
    with pytest.raises(UndefinedTable):
        Catalog(tmp_path).get("../users")


def cols(*extra):
    return {"table": "t", "columns": list(extra)}


ID = {"name": "id", "type": "BIGINT", "auto_increment": True}
NAME = {"name": "name", "type": "TEXT"}


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(cols({"name": "pk", "type": "BIGINT", "auto_increment": True}, NAME), id="first-not-id"),
        pytest.param(cols({"name": "id", "type": "TEXT", "auto_increment": True}, NAME), id="id-not-bigint"),
        pytest.param(cols({"name": "id", "type": "BIGINT"}, NAME), id="id-no-autoinc"),
        pytest.param(cols({"name": "id", "type": "BIGINT", "auto_increment": False}, NAME), id="id-autoinc-false"),
        pytest.param(cols(ID, {"name": "n", "type": "BIGINT", "auto_increment": True}), id="autoinc-on-other"),
        pytest.param(cols(ID, NAME, {"name": "NAME", "type": "BIGINT"}), id="duplicate-column"),
        pytest.param(cols(ID), id="only-one-column"),
        pytest.param(cols(), id="no-columns"),
        pytest.param(cols(ID, {"name": "x", "type": "VARCHAR"}), id="unsupported-type"),
        pytest.param(cols({**ID, "nullable": True}, NAME), id="id-nullable"),
        pytest.param(cols(ID, {"name": "x", "type": "TEXT", "nullable": "yes"}), id="nullable-not-bool"),
        pytest.param(cols(ID, {"name": "1x", "type": "TEXT"}), id="bad-identifier"),
        pytest.param(cols(ID, {"type": "TEXT"}), id="missing-name"),
        pytest.param(cols(ID, {"name": "x"}), id="missing-type"),
        pytest.param(cols(ID, {"name": "x", "type": "TEXT", "default": 1}), id="unknown-key"),
        pytest.param({"columns": [ID, NAME]}, id="missing-table"),
        pytest.param({"table": "t", "columns": "id"}, id="columns-not-list"),
        pytest.param([ID, NAME], id="not-object"),
    ],
)
def test_invalid_schema(data):
    with pytest.raises(InvalidSchemaDefinition) as ei:
        parse_schema(data)
    assert ei.value.sqlstate == "42P16"


def test_invalid_json_file(tmp_path):
    write_schema(tmp_path, "users", "{not json")
    with pytest.raises(InvalidSchemaDefinition):
        Catalog(tmp_path).get("users")


def test_table_name_must_match_file(tmp_path):
    write_schema(tmp_path, "users", {**USERS, "table": "people"})
    with pytest.raises(InvalidSchemaDefinition):
        Catalog(tmp_path).get("users")


def test_schema_is_frozen():
    s = TableSchema.from_dict(USERS)
    with pytest.raises(AttributeError):
        s.name = "x"  # type: ignore[misc]
