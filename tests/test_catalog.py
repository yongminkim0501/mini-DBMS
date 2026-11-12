import json
from pathlib import Path

import pytest

from minidb.catalog import Catalog, Column, TableSchema, parse_schema
from minidb.errors import InvalidSchemaDefinition, UndefinedTable
from minidb.storage import TableFile

USERS = {
    "oid": 16384,
    "table": "users",
    "columns": [
        {"name": "id", "type": "BIGINT", "auto_increment": True},
        {"name": "name", "type": "TEXT", "nullable": True},
        {"name": "age", "type": "BIGINT", "nullable": True},
    ],
}


def table(oid, name, *data_cols):
    cols = [{"name": "id", "type": "BIGINT", "auto_increment": True}]
    cols += [{"name": c, "type": "TEXT"} for c in (data_cols or ("v",))]
    return {"oid": oid, "table": name, "columns": cols}


def write_schema(tmp_path, filename, data):
    d = tmp_path / "schema"
    d.mkdir(exist_ok=True)
    p = d / filename
    p.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


# ------------------------------------------------------------------ 로딩


def test_load_users(tmp_path):
    write_schema(tmp_path, "16384.json", USERS)
    schema = Catalog(tmp_path).get("users")
    assert schema.name == "users"
    assert schema.oid == 16384
    assert schema.columns[0] == Column("id", "BIGINT", nullable=False, auto_increment=True)
    assert [c.name for c in schema.data_columns] == ["name", "age"]
    assert schema.column_names == ("id", "name", "age")


def test_example_schema_in_repo_is_valid():
    root = Path(__file__).resolve().parents[1] / "db"
    cat = Catalog(root)
    assert cat.get("users").oid == 16384
    assert cat.data_path("users") == root / "data" / "16384.tbl"


def test_names_are_not_normalized(tmp_path):
    data = {
        "oid": 1,
        "table": "Users",
        "columns": [
            {"name": "id", "type": "bigint", "auto_increment": True},  # 타입은 대소문자 무시
            {"name": "UserName", "type": "Text"},
            {"name": "ÄgeΣ", "type": "BIGINT"},
        ],
    }
    write_schema(tmp_path, "1.json", data)
    cat = Catalog(tmp_path)
    schema = cat.get("Users")  # JSON 에 적힌 그대로가 이름
    assert schema.name == "Users"
    assert schema.column_names == ("id", "UserName", "ÄgeΣ")
    assert schema.columns[1].type == "TEXT"


def test_exact_match_lookup(tmp_path):
    write_schema(tmp_path, "a.json", USERS)
    cat = Catalog(tmp_path)
    for wrong in ("Users", "USERS", " users", "users "):
        with pytest.raises(UndefinedTable):
            cat.get(wrong)
    assert cat.get("users").name == "users"


def test_users_and_Users_coexist_in_separate_files(tmp_path):
    """대소문자를 구분하지 않는 파일 시스템(macOS)에서도 두 테이블이 각자 다른 파일을 쓴다."""
    write_schema(tmp_path, "100.json", table(100, "users", "v"))
    write_schema(tmp_path, "101.json", table(101, "Users", "v"))
    cat = Catalog(tmp_path)
    lower, upper = cat.get("users"), cat.get("Users")
    assert (lower.oid, upper.oid) == (100, 101)
    p_lower, p_upper = cat.data_path("users"), cat.data_path("Users")
    assert p_lower.name == "100.tbl" and p_upper.name == "101.tbl"

    with TableFile(p_lower, lower) as t:
        t.append(("lower",))
    with TableFile(p_upper, upper) as t:
        t.append(("UPPER-1",))
        t.append(("UPPER-2",))

    with TableFile(p_lower, lower) as t:
        assert [r for _, r in t.scan()] == [(1, "lower")]
    with TableFile(p_upper, upper) as t:
        assert [r for _, r in t.scan()] == [(1, "UPPER-1"), (2, "UPPER-2")]
    assert sorted(p.name for p in cat.data_dir.iterdir()) == ["100.tbl", "101.tbl"]


def test_korean_table_name_and_arbitrary_schema_filename(tmp_path):
    # 스키마 파일 이름은 테이블 이름과 무관해도 된다
    write_schema(tmp_path, "whatever-name.json", table(200, "회원", "이름"))
    write_schema(tmp_path, "zzz.json", table(201, "users회원", "이름"))
    cat = Catalog(tmp_path)
    assert cat.get("회원").column_names == ("id", "이름")
    assert cat.get("users회원").oid == 201
    assert cat.data_path("회원").name == "200.tbl"
    assert cat.schema_file("회원").name == "whatever-name.json"
    assert [s.name for s in cat.tables()] == ["회원", "users회원"]


def test_nfc_nfd_names_are_distinct_but_files_unaffected(tmp_path):
    import unicodedata

    nfc = unicodedata.normalize("NFC", "회원")
    nfd = unicodedata.normalize("NFD", "회원")
    assert nfc != nfd
    write_schema(tmp_path, "1.json", table(1, nfc))
    cat = Catalog(tmp_path)
    assert cat.get(nfc).oid == 1
    with pytest.raises(UndefinedTable):
        cat.get(nfd)  # 이름 비교는 카탈로그가 정확히 한다 (파일 시스템이 관여하지 않음)


def test_non_json_files_ignored_and_missing_dir(tmp_path):
    with pytest.raises(UndefinedTable):
        Catalog(tmp_path).get("users")  # schema 디렉터리 없음 → 빈 카탈로그
    write_schema(tmp_path, "16384.json", USERS)
    (tmp_path / "schema" / "README.txt").write_text("not a schema")
    (tmp_path / "schema" / "16384.json.bak").write_text("{broken")
    assert Catalog(tmp_path).get("users").oid == 16384


# ------------------------------------------------------------------ 중복


def test_duplicate_table_name_rejected(tmp_path):
    write_schema(tmp_path, "1.json", table(1, "users"))
    write_schema(tmp_path, "2.json", table(2, "users"))
    with pytest.raises(InvalidSchemaDefinition, match="defined more than once") as ei:
        Catalog(tmp_path).get("users")
    assert "1.json" in ei.value.message and "2.json" in ei.value.message


def test_duplicate_oid_rejected(tmp_path):
    write_schema(tmp_path, "a.json", table(7, "a"))
    write_schema(tmp_path, "b.json", table(7, "b"))
    with pytest.raises(InvalidSchemaDefinition, match="oid 7 is used more than once"):
        Catalog(tmp_path).get("a")


def test_case_variants_are_not_duplicates(tmp_path):
    write_schema(tmp_path, "1.json", table(1, "users"))
    write_schema(tmp_path, "2.json", table(2, "Users"))
    assert len(Catalog(tmp_path).tables()) == 2


# ------------------------------------------------------------------ 캐시


def test_cache_and_reload(tmp_path):
    p = write_schema(tmp_path, "16384.json", USERS)
    cat = Catalog(tmp_path)
    a = cat.get("users")
    p.unlink()  # 이미 읽었으므로 파일이 없어도 동작
    assert cat.get("users") is a
    # 다른 인스턴스는 캐시를 공유하지 않는다
    with pytest.raises(UndefinedTable):
        Catalog(tmp_path).get("users")
    write_schema(tmp_path, "5.json", table(5, "later"))
    with pytest.raises(UndefinedTable):
        cat.get("later")
    cat.reload()
    assert cat.get("later").oid == 5


def test_undefined_table(tmp_path):
    write_schema(tmp_path, "1.json", USERS)
    with pytest.raises(UndefinedTable) as ei:
        Catalog(tmp_path).get("nope")
    assert ei.value.sqlstate == "42P01"
    with pytest.raises(UndefinedTable):
        Catalog(tmp_path).get("../users")


# ------------------------------------------------------------------ 검증


def test_nullable_default_true_and_false():
    s = parse_schema(
        {
            "oid": 1,
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


def test_any_name_without_nul_is_allowed():
    # 이름은 정규화·형식 검사를 하지 않는다 (인용 식별자로 쓸 수 있는 이름이면 된다)
    s = parse_schema(table(1, "my table-1", "a b", "1x", "$", "A", "a"))
    assert s.name == "my table-1"
    assert s.column_names == ("id", "a b", "1x", "$", "A", "a")  # "A" 와 "a" 는 다른 컬럼


def cols(*extra, oid=1):
    return {"oid": oid, "table": "t", "columns": list(extra)}


ID = {"name": "id", "type": "BIGINT", "auto_increment": True}
NAME = {"name": "name", "type": "TEXT"}


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(cols({"name": "pk", "type": "BIGINT", "auto_increment": True}, NAME), id="first-not-id"),
        pytest.param(cols({"name": "ID", "type": "BIGINT", "auto_increment": True}, NAME), id="first-ID-uppercase"),
        pytest.param(cols({"name": "id", "type": "TEXT", "auto_increment": True}, NAME), id="id-not-bigint"),
        pytest.param(cols({"name": "id", "type": "BIGINT"}, NAME), id="id-no-autoinc"),
        pytest.param(cols({"name": "id", "type": "BIGINT", "auto_increment": False}, NAME), id="id-autoinc-false"),
        pytest.param(cols(ID, {"name": "n", "type": "BIGINT", "auto_increment": True}), id="autoinc-on-other"),
        pytest.param(cols(ID, NAME, {"name": "name", "type": "BIGINT"}), id="duplicate-column"),
        pytest.param(cols(ID), id="only-one-column"),
        pytest.param(cols(), id="no-columns"),
        pytest.param(cols(ID, {"name": "x", "type": "VARCHAR"}), id="unsupported-type"),
        pytest.param(cols({**ID, "nullable": True}, NAME), id="id-nullable"),
        pytest.param(cols(ID, {"name": "x", "type": "TEXT", "nullable": "yes"}), id="nullable-not-bool"),
        pytest.param(cols(ID, {"name": "", "type": "TEXT"}), id="empty-name"),
        pytest.param(cols(ID, {"name": "a\x00b", "type": "TEXT"}), id="nul-in-name"),
        pytest.param(cols(ID, {"name": "a" * 64, "type": "TEXT"}), id="name-too-long"),
        pytest.param(cols(ID, {"name": "가" * 22, "type": "TEXT"}), id="name-too-long-utf8"),
        pytest.param(cols(ID, {"type": "TEXT"}), id="missing-name"),
        pytest.param(cols(ID, {"name": "x"}), id="missing-type"),
        pytest.param(cols(ID, {"name": "x", "type": "TEXT", "default": 1}), id="unknown-key"),
        pytest.param({"oid": 1, "columns": [ID, NAME]}, id="missing-table"),
        pytest.param({"oid": 1, "table": "t\x00", "columns": [ID, NAME]}, id="nul-in-table"),
        pytest.param({"table": "t", "columns": [ID, NAME]}, id="missing-oid"),
        pytest.param(cols(ID, NAME, oid=0), id="oid-zero"),
        pytest.param(cols(ID, NAME, oid=-1), id="oid-negative"),
        pytest.param(cols(ID, NAME, oid=True), id="oid-bool"),
        pytest.param(cols(ID, NAME, oid="16384"), id="oid-string"),
        pytest.param(cols(ID, NAME, oid=1.0), id="oid-float"),
        pytest.param(cols(ID, NAME, oid=2**32), id="oid-too-large"),
        pytest.param({"oid": 1, "table": "t", "columns": "id"}, id="columns-not-list"),
        pytest.param([ID, NAME], id="not-object"),
    ],
)
def test_invalid_schema(data):
    with pytest.raises(InvalidSchemaDefinition) as ei:
        parse_schema(data)
    assert ei.value.sqlstate == "42P16"


def test_invalid_json_file_names_file(tmp_path):
    write_schema(tmp_path, "broken.json", "{not json")
    with pytest.raises(InvalidSchemaDefinition, match="broken.json"):
        Catalog(tmp_path).get("users")


def test_invalid_schema_file_names_file(tmp_path):
    write_schema(tmp_path, "bad.json", {**USERS, "oid": 0})
    with pytest.raises(InvalidSchemaDefinition, match="bad.json"):
        Catalog(tmp_path).get("users")


def test_schema_is_frozen():
    s = TableSchema.from_dict(USERS)
    with pytest.raises(AttributeError):
        s.name = "x"  # type: ignore[misc]
