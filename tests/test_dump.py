import io
import json
import shutil
import struct
from pathlib import Path

from minidb.catalog import Catalog
from minidb.storage import RECORD_HEADER_SIZE as RH
from minidb.storage import TableFile
from minidb.tools.dump import dump, main

REPO_SCHEMA = Path(__file__).resolve().parents[1] / "db" / "schema" / "16384.json"


def make_db(tmp_path):
    (tmp_path / "schema").mkdir()
    shutil.copy(REPO_SCHEMA, tmp_path / "schema" / "16384.json")
    cat = Catalog(tmp_path)
    with TableFile(cat.data_path("users"), cat.get("users")) as t:
        t.append_many([("alice", 30), (None, None), ("홍길동", 7)])
    return tmp_path


def test_dump_output(tmp_path):
    db = make_db(tmp_path)
    out = io.StringIO()
    assert dump("users", str(db), out=out) == 3
    text = out.getvalue()
    assert "b'MDB1'" in text
    assert "format_version : 2" in text
    assert "column_count   : 3" in text
    assert "records        : 3" in text
    assert "next_id        : 4" in text
    assert "(2, NULL, NULL)" in text
    assert "(3, '홍길동', 7)" in text
    assert "          16  OK           0x00" in text
    assert "open           : ok" in text


def test_dump_limit(tmp_path):
    db = make_db(tmp_path)
    out = io.StringIO()
    dump("users", str(db), limit=1, out=out)
    assert "... (2 more)" in out.getvalue()


def test_dump_missing_table(tmp_path, capsys):
    assert main(["nope", "--db", str(tmp_path)]) == 1
    assert "42P01" in capsys.readouterr().err


def test_dump_does_not_modify_file(tmp_path):
    db = make_db(tmp_path)
    p = Catalog(db).data_path("users")
    with open(p, "ab") as f:
        f.write(b"\x01\x02")
    before = p.read_bytes()
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        dump("users", str(db), out=io.StringIO())
    assert p.read_bytes() == before
    assert sorted(x.name for x in p.parent.iterdir()) == ["16384.tbl"]  # quarantine 파일도 만들지 않음


def test_dump_shows_bad_crc(tmp_path):
    db = make_db(tmp_path)
    p = Catalog(db).data_path("users")
    raw = bytearray(p.read_bytes())
    # 두 번째 레코드(offset = 16 + 첫 레코드 크기)의 본문 마지막 바이트 반전
    (len1,) = struct.unpack_from("<I", raw, 16)
    off2 = 16 + RH + len1
    (len2,) = struct.unpack_from("<I", raw, off2)
    raw[off2 + RH + len2 - 1] ^= 0xFF
    p.write_bytes(bytes(raw))
    out = io.StringIO()
    assert dump("users", str(db), out=out) == 3
    text = out.getvalue()
    assert "open           : FAILED" in text and "XX001" in text
    assert f"{off2:>12}  BAD(body)" in text
    assert "(3, '홍길동', 7)" in text  # body 만 깨졌으므로 뒤 레코드는 계속 보여준다
    assert "bad 1" in text


def test_dump_table_argument_follows_sql_identifier_rules(tmp_path):
    db = make_db(tmp_path)  # users (oid 16384)
    upper = {
        "oid": 16385,
        "table": "Users",
        "columns": [{"name": "id", "type": "BIGINT", "auto_increment": True}, {"name": "v", "type": "TEXT"}],
    }
    (db / "schema" / "16385.json").write_text(json.dumps(upper), encoding="utf-8")
    cat = Catalog(db)
    with TableFile(cat.data_path("Users"), cat.get("Users")) as t:
        t.append(("from-Users",))

    out = io.StringIO()
    dump("USERS", str(db), out=out)  # 비인용 → users 로 접힘
    assert "table          : users  (oid 16384)" in out.getvalue()
    assert "16384.tbl" in out.getvalue()

    out = io.StringIO()
    dump('"Users"', str(db), out=out)  # 인용 → 그대로
    text = out.getvalue()
    assert "table          : Users  (oid 16385)" in text
    assert "16385.tbl" in text and "'from-Users'" in text


def test_resolve_table_arg():
    from minidb.tools.dump import resolve_table_arg

    assert resolve_table_arg("Users회원") == "users회원"
    assert resolve_table_arg('"Users회원"') == "Users회원"
    assert resolve_table_arg('"a""b"') == 'a"b'
    assert resolve_table_arg('"') == '"'
