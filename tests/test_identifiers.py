import json

import pytest

from minidb.catalog import Catalog
from minidb.errors import UndefinedTable
from minidb.identifiers import fold_identifier, identifier_too_long, is_valid_unquoted_identifier
from minidb.sql.parser import parse_statement


@pytest.mark.parametrize(
    "raw, folded",
    [
        ("Users", "users"),
        ("Users회원", "users회원"),
        ("이름", "이름"),
        ("Ä", "Ä"),  # 비 ASCII 대문자는 변환하지 않음
        ("Σίσυφος", "Σίσυφος"),
        ("ÄBC", "Äbc"),  # ASCII 부분만 접힘
        ("İSTANBUL", "İstanbul"),  # str.lower() 는 'i̇stanbul' (결합 문자 추가) 로 바꾼다
        ("ABC_xyz$9", "abc_xyz$9"),
    ],
)
def test_fold_identifier(raw, folded):
    assert fold_identifier(raw) == folded


def test_fold_differs_from_str_lower():
    for s in ("Ä", "Σ", "İ", "Ｆ"):  # 전각 Ｆ 포함
        assert fold_identifier(s) == s != s.lower()


@pytest.mark.parametrize("name", ["a", "_x", "이름", "회원1", "a$b", "Ä", "😀", "x1_$", "名前"])
def test_valid_unquoted(name):
    assert is_valid_unquoted_identifier(name)


@pytest.mark.parametrize("name", ["", "1a", "$a", "a-b", "a b", "a.b", 'a"b', "a;b", "a\x00"])
def test_invalid_unquoted(name):
    assert not is_valid_unquoted_identifier(name)


def test_identifier_too_long():
    assert not identifier_too_long("a" * 63)
    assert identifier_too_long("a" * 64)
    assert not identifier_too_long("가" * 21)
    assert identifier_too_long("가" * 22)
    assert not identifier_too_long("😀" * 15)  # 60바이트


def test_catalog_and_sql_use_same_rule(tmp_path):
    """스키마 JSON 의 이름과 SQL 의 비인용 식별자가 같은 규칙으로 정규화되어 매칭된다."""
    (tmp_path / "schema").mkdir()
    schema_json = {
        "table": "Users회원",
        "columns": [
            {"name": "ID", "type": "BIGINT", "auto_increment": True},
            {"name": "이름", "type": "TEXT"},
            {"name": "Ägé", "type": "BIGINT"},
        ],
    }
    (tmp_path / "schema" / "users회원.json").write_text(json.dumps(schema_json, ensure_ascii=False), encoding="utf-8")
    cat = Catalog(tmp_path)

    stmt = parse_statement("SELECT 이름, ÄGé FROM USERS회원 WHERE iD = 1")
    schema = cat.get(stmt.table.name)
    assert schema.name == "users회원"
    assert set(stmt.columns) <= set(schema.column_names)  # 'ÄGé' → 'Ägé' (Ä, é 는 그대로, G 만 접힘)
    assert stmt.where[0].column in schema.column_names

    # 인용하면 접지 않으므로 다른 이름 → 없음 (PostgreSQL 과 동일)
    quoted = parse_statement('SELECT * FROM "Users회원"')
    with pytest.raises(UndefinedTable):
        cat.get(quoted.table.name)
    # 대소문자를 정확히 맞춰 인용하면 찾는다
    assert cat.get(parse_statement('SELECT * FROM "users회원"').table.name) is schema
