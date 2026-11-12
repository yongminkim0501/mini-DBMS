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


def test_catalog_names_match_sql_identifiers(tmp_path):
    """카탈로그는 JSON 의 이름을 그대로 쓰고, SQL 쪽 접기는 렉서만 한다. 둘을 이으면 PostgreSQL 과 같은 규칙이 된다."""
    (tmp_path / "schema").mkdir()

    def write(oid, name, *cols):
        doc = {
            "oid": oid,
            "table": name,
            "columns": [{"name": "id", "type": "BIGINT", "auto_increment": True}]
            + [{"name": c, "type": "TEXT"} for c in cols],
        }
        (tmp_path / "schema" / f"{oid}.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    write(1, "users회원", "이름", "Ägé")  # SQL 의 비인용 Users회원 → users회원 으로 찾는 테이블
    write(2, "Users회원", "x")  # SQL 의 "Users회원" 으로만 찾는 테이블
    cat = Catalog(tmp_path)

    stmt = parse_statement("SELECT 이름, ÄGé FROM USERS회원 WHERE iD = 1")
    schema = cat.get(stmt.table.name)
    assert schema.oid == 1
    assert set(stmt.columns) <= set(schema.column_names)  # 'ÄGé' → 'Ägé' (Ä, é 는 그대로, G 만 접힘)
    assert stmt.where[0].column in schema.column_names

    assert cat.get(parse_statement('SELECT * FROM "Users회원"').table.name).oid == 2
    assert cat.get(parse_statement('SELECT * FROM "users회원"').table.name).oid == 1
    with pytest.raises(UndefinedTable):
        cat.get(parse_statement('SELECT * FROM "USERS회원"').table.name)
