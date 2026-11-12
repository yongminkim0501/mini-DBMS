import io
import shutil
from pathlib import Path

from minidb.catalog import Catalog
from minidb.storage import TableFile
from minidb.tools.dump import dump, main

REPO_SCHEMA = Path(__file__).resolve().parents[1] / "db" / "schema" / "users.json"


def make_db(tmp_path):
    (tmp_path / "schema").mkdir()
    shutil.copy(REPO_SCHEMA, tmp_path / "schema" / "users.json")
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
    assert "          16  0x00" in text


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
