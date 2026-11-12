import struct
import warnings

import pytest

from minidb.catalog import parse_schema
from minidb.errors import (
    CharacterNotInRepertoire,
    DataCorrupted,
    DatatypeMismatch,
    InvalidParameterValue,
    NotNullViolation,
    NumericValueOutOfRange,
    ProgramLimitExceeded,
    SequenceGeneratorLimitExceeded,
)
from minidb.storage import BIGINT_MAX, BIGINT_MIN, FLAG_DELETED, HEADER_SIZE, MAGIC, TableFile

USERS = parse_schema(
    {
        "table": "users",
        "columns": [
            {"name": "id", "type": "BIGINT", "auto_increment": True},
            {"name": "name", "type": "TEXT", "nullable": True},
            {"name": "age", "type": "BIGINT", "nullable": True},
        ],
    }
)

STRICT = parse_schema(
    {
        "table": "strict",
        "columns": [
            {"name": "id", "type": "BIGINT", "auto_increment": True},
            {"name": "name", "type": "TEXT", "nullable": False},
            {"name": "age", "type": "BIGINT", "nullable": False},
        ],
    }
)


def wide_schema(n):
    return parse_schema(
        {
            "table": "wide",
            "columns": [{"name": "id", "type": "BIGINT", "auto_increment": True}]
            + [{"name": f"c{i}", "type": "BIGINT" if i % 2 else "TEXT"} for i in range(n)],
        }
    )


@pytest.fixture
def path(tmp_path):
    return tmp_path / "data" / "users.tbl"


@pytest.fixture
def tf(path):
    with TableFile(path, USERS) as t:
        yield t


# ------------------------------------------------------------------ 왕복


@pytest.mark.parametrize(
    "values",
    [
        ("alice", 30),
        (None, 30),
        ("bob", None),
        (None, None),
        ("", 0),
        ("홍길동", 25),
        ("😀👍🏽 이모지", 1),
        ("x", BIGINT_MAX),
        ("y", BIGINT_MIN),
        ("a\x00b", -1),
    ],
)
def test_roundtrip(tf, values):
    rid, off = tf.append(values)
    assert tf.read_at(off) == (rid, *values)
    assert list(tf.scan()) == [(off, (rid, *values))]


def test_roundtrip_after_reopen(path):
    rows = [("alice", 30), (None, None), ("", BIGINT_MIN), ("한글😀", BIGINT_MAX)]
    with TableFile(path, USERS) as t:
        res = t.append_many(rows)
    with TableFile(path, USERS) as t:
        for (rid, off), vals in zip(res, rows):
            assert t.read_at(off) == (rid, *vals)
        assert [r for _, r in t.scan()] == [(rid, *v) for (rid, _), v in zip(res, rows)]


def test_header_written(path):
    TableFile(path, USERS).close()
    raw = path.read_bytes()
    assert len(raw) == HEADER_SIZE
    assert raw == MAGIC + struct.pack("<HH", 1, 3) + b"\x00" * 8


def test_exact_record_bytes(path):
    with TableFile(path, USERS) as t:
        _, off = t.append(("홍", None))
    raw = path.read_bytes()[off:]
    body = b"\x00" + struct.pack("<q", 1) + b"\x02" + struct.pack("<I", 3) + "홍".encode()
    assert raw == struct.pack("<I", len(body)) + body


@pytest.mark.parametrize("n, bitmap_len", [(7, 1), (8, 1), (9, 2), (16, 2), (17, 3)])
def test_null_bitmap_boundary(tmp_path, n, bitmap_len):
    schema = wide_schema(n)
    p = tmp_path / "w.tbl"
    with TableFile(p, schema) as t:
        all_null = (None,) * n
        _, off0 = t.append(all_null)
        last_only = (None,) * (n - 1) + (("v" if (n - 1) % 2 == 0 else 7),)
        _, off1 = t.append(last_only)
        assert t.read_at(off0) == (1, *all_null)
        assert t.read_at(off1) == (2, *last_only)
    raw = p.read_bytes()
    (length,) = struct.unpack_from("<I", raw, off0)
    # 모든 컬럼 NULL 이면 본문 = flags(1) + id(8) + bitmap
    assert length == 1 + 8 + bitmap_len
    bitmap = raw[off0 + 4 + 9 : off0 + 4 + 9 + bitmap_len]
    assert int.from_bytes(bitmap, "little") == (1 << n) - 1
    # 마지막 컬럼만 값이 있으면 그 비트만 0
    bitmap1 = raw[off1 + 4 + 9 : off1 + 4 + 9 + bitmap_len]
    assert int.from_bytes(bitmap1, "little") == (1 << (n - 1)) - 1


# ------------------------------------------------------------------ id / offset


def test_ids_start_at_one_and_increase(tf):
    assert tf.next_id == 1
    res = [tf.append((f"u{i}", i)) for i in range(5)]
    assert [rid for rid, _ in res] == [1, 2, 3, 4, 5]
    assert tf.next_id == 6
    res2 = tf.append_many([("a", 1), ("b", 2)])
    assert [rid for rid, _ in res2] == [6, 7]
    offs = [o for _, o in res + res2]
    assert offs[0] == HEADER_SIZE
    assert offs == sorted(offs)


def test_offsets_read_back(tf):
    rows = [(f"name{i}", i * 3) for i in range(200)]
    res = tf.append_many(rows)
    for (rid, off), vals in reversed(list(zip(res, rows))):
        assert tf.read_at(off) == (rid, *vals)


def test_reopen_preserves_data_and_next_id(path):
    with TableFile(path, USERS) as t:
        t.append(("a", 1))
        t.append(("b", 2))
    with TableFile(path, USERS) as t:
        assert t.next_id == 3
        assert [r for _, r in t.scan()] == [(1, "a", 1), (2, "b", 2)]
        assert t.append(("c", 3))[0] == 3
    with TableFile(path, USERS) as t:
        assert t.next_id == 4
        assert len(list(t.scan())) == 3


def test_read_your_writes_without_flush(tf, path):
    _, off = tf.append(("fresh", 1))
    # 아직 flush 하지 않았으므로 디스크에는 헤더만 있을 수도 있다
    assert tf.read_at(off) == (1, "fresh", 1)
    _, off2 = tf.append(("fresh2", 2))
    assert list(tf.scan()) == [(off, (1, "fresh", 1)), (off2, (2, "fresh2", 2))]


def test_scan_snapshot_and_interleaved_reads(tf):
    res = tf.append_many([("a", 1), ("b", 2), ("c", 3)])
    it = tf.scan()
    first = next(it)
    assert tf.read_at(res[2][1]) == (3, "c", 3)
    tf.append(("d", 4))  # scan 시작 이후 추가된 행은 보이지 않음
    rest = list(it)
    assert [first, *rest] == [(o, (i, n, a)) for (i, o), (n, a) in zip(res, [("a", 1), ("b", 2), ("c", 3)])]


def test_scan_spans_multiple_chunks(tf):
    big = "가" * 200_000  # 600KB → 1MiB 청크 경계를 넘나든다
    rows = [(big, i) for i in range(5)]
    tf.append_many(rows)
    got = [r for _, r in tf.scan()]
    assert got == [(i + 1, big, i) for i in range(5)]


# ------------------------------------------------------------------ 검증 에러


@pytest.mark.parametrize(
    "values, exc, code",
    [
        (("a",), InvalidParameterValue, "22023"),
        (("a", 1, 2), InvalidParameterValue, "22023"),
        ("ab", InvalidParameterValue, "22023"),
        (("a", "30"), DatatypeMismatch, "42804"),
        (("a", 1.0), DatatypeMismatch, "42804"),
        (("a", True), DatatypeMismatch, "42804"),
        (("a", False), DatatypeMismatch, "42804"),
        ((1, 1), DatatypeMismatch, "42804"),
        ((b"bytes", 1), DatatypeMismatch, "42804"),
        (("a", BIGINT_MAX + 1), NumericValueOutOfRange, "22003"),
        (("a", BIGINT_MIN - 1), NumericValueOutOfRange, "22003"),
        (("\ud800", 1), CharacterNotInRepertoire, "22021"),
    ],
)
def test_validation_errors(tf, path, values, exc, code):
    before = tf.next_id
    with pytest.raises(exc) as ei:
        tf.append(values)
    assert ei.value.sqlstate == code
    assert tf.next_id == before
    tf.flush()
    assert path.stat().st_size == HEADER_SIZE


def test_not_null_violation(tmp_path):
    with TableFile(tmp_path / "s.tbl", STRICT) as t:
        with pytest.raises(NotNullViolation) as ei:
            t.append((None, 1))
        assert ei.value.sqlstate == "23502"
        assert 'column "name"' in ei.value.message
        with pytest.raises(NotNullViolation):
            t.append(("a", None))
        assert t.append(("a", 1)) == (1, HEADER_SIZE)


def test_text_limit_is_bytes(tmp_path):
    with TableFile(tmp_path / "t.tbl", USERS, max_text_bytes=6) as t:
        t.append(("가나", 1))  # 6바이트: 허용
        with pytest.raises(ProgramLimitExceeded) as ei:
            t.append(("가나a", 1))  # 3글자지만 7바이트
        assert ei.value.sqlstate == "54000"
        with pytest.raises(ProgramLimitExceeded):
            t.append(("abcdefg", 1))


def test_append_many_atomic(tf, path):
    tf.append(("first", 1))
    tf.flush()
    size = path.stat().st_size
    before = tf.next_id
    with pytest.raises(DatatypeMismatch):
        tf.append_many([("ok", 1), ("ok2", 2), ("bad", "x"), ("ok3", 3)])
    tf.flush()
    assert path.stat().st_size == size
    assert tf.size == size
    assert tf.next_id == before
    assert [r for _, r in tf.scan()] == [(1, "first", 1)]
    assert tf.append(("after", 2))[0] == before


def test_append_many_accepts_generator_and_empty(tf):
    assert tf.append_many([]) == []
    res = tf.append_many((f"g{i}", i) for i in range(3))
    assert [rid for rid, _ in res] == [1, 2, 3]


# ------------------------------------------------------------------ 손상/복구


def test_truncated_tail_recovered(path):
    with TableFile(path, USERS) as t:
        res = t.append_many([("a", 1), ("b", 2), ("ccc", 3)])
    full = path.stat().st_size
    with open(path, "r+b") as f:
        f.truncate(full - 3)
    with pytest.warns(RuntimeWarning, match="truncated"):
        t = TableFile(path, USERS)
    with t:
        assert path.stat().st_size == res[2][1]
        assert [r for _, r in t.scan()] == [(1, "a", 1), (2, "b", 2)]
        assert t.next_id == 3
        rid, off = t.append(("new", 9))
        assert (rid, off) == (3, res[2][1])
        assert t.read_at(off) == (3, "new", 9)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with TableFile(path, USERS) as t:
            assert len(list(t.scan())) == 3


@pytest.mark.parametrize("extra", [b"\x01", b"\x01\x02\x03", struct.pack("<I", 100) + b"\x00" * 10])
def test_partial_length_or_body_tail(path, extra):
    with TableFile(path, USERS) as t:
        t.append(("a", 1))
    good = path.stat().st_size
    with open(path, "ab") as f:
        f.write(extra)
    with pytest.warns(RuntimeWarning):
        with TableFile(path, USERS) as t:
            assert t.next_id == 2
    assert path.stat().st_size == good


def test_readonly_does_not_truncate(path):
    with TableFile(path, USERS) as t:
        t.append(("a", 1))
    with open(path, "ab") as f:
        f.write(b"\x09\x00")
    size = path.stat().st_size
    with pytest.warns(RuntimeWarning, match="readonly"):
        with TableFile(path, USERS, readonly=True) as t:
            assert [r for _, r in t.scan()] == [(1, "a", 1)]
            with pytest.raises(ValueError):
                t.append(("b", 2))
    assert path.stat().st_size == size


def test_bad_magic(path):
    TableFile(path, USERS).close()
    raw = bytearray(path.read_bytes())
    raw[0:4] = b"XXXX"
    path.write_bytes(bytes(raw))
    with pytest.raises(DataCorrupted) as ei:
        TableFile(path, USERS)
    assert ei.value.sqlstate == "XX001"


def test_bad_version(path):
    TableFile(path, USERS).close()
    raw = bytearray(path.read_bytes())
    raw[4:6] = struct.pack("<H", 2)
    path.write_bytes(bytes(raw))
    with pytest.raises(DataCorrupted):
        TableFile(path, USERS)


def test_column_count_mismatch(path):
    TableFile(path, USERS).close()
    with pytest.raises(DataCorrupted, match="column count"):
        TableFile(path, wide_schema(5))


@pytest.mark.parametrize("size", [1, 4, 15])
def test_short_file(path, size):
    path.parent.mkdir(parents=True)
    path.write_bytes(MAGIC[:size] + b"\x00" * max(0, size - 4))
    with pytest.raises(DataCorrupted):
        TableFile(path, USERS)


def test_empty_file_is_initialized(path):
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")
    with TableFile(path, USERS) as t:
        assert t.next_id == 1
    assert path.stat().st_size == HEADER_SIZE


def test_length_below_minimum_is_corrupted(path):
    with TableFile(path, USERS) as t:
        _, off = t.append(("a", 1))
        t.append(("b", 2))
    raw = bytearray(path.read_bytes())
    raw[off : off + 4] = struct.pack("<I", 3)
    path.write_bytes(bytes(raw))
    with pytest.raises(DataCorrupted):
        TableFile(path, USERS)


def test_non_increasing_id_is_corrupted(path):
    with TableFile(path, USERS) as t:
        t.append(("a", 1))
        _, off2 = t.append(("b", 2))
    raw = bytearray(path.read_bytes())
    raw[off2 + 5 : off2 + 13] = struct.pack("<q", 1)
    path.write_bytes(bytes(raw))
    with pytest.raises(DataCorrupted, match="not greater"):
        TableFile(path, USERS)


# ------------------------------------------------------------------ deleted


def test_deleted_flag_respected(path):
    with TableFile(path, USERS) as t:
        res = t.append_many([("a", 1), ("b", 2), ("c", 3)])
    raw = bytearray(path.read_bytes())
    raw[res[1][1] + 4] |= FLAG_DELETED  # flags 바이트는 length(4) 바로 뒤
    path.write_bytes(bytes(raw))
    with TableFile(path, USERS) as t:
        assert t.read_at(res[1][1]) is None
        assert t.read_at(res[0][1]) == (1, "a", 1)
        assert [r for _, r in t.scan()] == [(1, "a", 1), (3, "c", 3)]
        assert [f for _, f, _ in t.records()] == [0, FLAG_DELETED, 0]
        # deleted 레코드의 id 도 재사용하지 않는다
        assert t.next_id == 4


# ------------------------------------------------------------------ read_at 경계


def test_read_at_out_of_range(tf):
    _, off = tf.append(("a", 1))
    for bad in (-1, 0, HEADER_SIZE - 1, tf.size, tf.size + 100):
        with pytest.raises(ValueError):
            tf.read_at(bad)
    with pytest.raises(ValueError):
        tf.read_at("16")  # type: ignore[arg-type]


def test_read_at_non_boundary(tf):
    res = tf.append_many([("hello world", 123456789), ("가나다라", 42), (None, 7)])
    starts = {o for _, o in res}
    for off in range(HEADER_SIZE, tf.size):
        if off in starts:
            continue
        with pytest.raises((DataCorrupted, ValueError)):
            tf.read_at(off)


# ------------------------------------------------------------------ 생명주기


def test_close_idempotent_and_closed_ops(path):
    t = TableFile(path, USERS)
    _, off = t.append(("a", 1))
    t.close()
    t.close()
    assert t.closed
    for op in (lambda: t.append(("b", 2)), lambda: t.read_at(off), lambda: t.scan(), lambda: t.flush()):
        with pytest.raises(ValueError):
            op()
    with TableFile(path, USERS) as t2:
        assert t2.read_at(off) == (1, "a", 1)


def test_flush_durable(tf, path):
    tf.append(("a", 1))
    tf.flush(durable=True)
    assert path.stat().st_size == tf.size


def test_sequence_limit(tf, path):
    tf._next_id = BIGINT_MAX  # 시퀀스 끝 직전으로 강제
    rid, _ = tf.append(("last", 1))
    assert rid == BIGINT_MAX
    size = tf.size
    with pytest.raises(SequenceGeneratorLimitExceeded) as ei:
        tf.append(("overflow", 2))
    assert ei.value.sqlstate == "2200H"
    assert tf.size == size
    assert tf.next_id == BIGINT_MAX + 1
