import struct
import warnings
import zlib

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
from minidb.storage import (
    BIGINT_MAX,
    BIGINT_MIN,
    FLAG_DELETED,
    FORMAT_VERSION,
    HEADER_SIZE,
    MAGIC,
    RECORD_HEADER_SIZE as RH,
    TableFile,
)

USERS = parse_schema(
    {
        "oid": 1,
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
        "oid": 2,
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
            "oid": 3,
            "table": "wide",
            "columns": [{"name": "id", "type": "BIGINT", "auto_increment": True}]
            + [{"name": f"c{i}", "type": "BIGINT" if i % 2 else "TEXT"} for i in range(n)],
        }
    )


def make_record(body, *, length=None):
    """본문으로 v2 레코드 바이트를 만든다 (length 를 바꿔도 CRC 는 일관되게 다시 계산)."""
    length = len(body) if length is None else length
    len_crc = struct.pack("<II", length, zlib.crc32(body))
    return len_crc + struct.pack("<I", zlib.crc32(len_crc)) + body


def record_at(raw, off):
    """raw 에서 off 위치 레코드의 (전체 크기, 본문) 을 반환한다."""
    (length,) = struct.unpack_from("<I", raw, off)
    return RH + length, bytes(raw[off + RH : off + RH + length])


def rewrite_record(path, off, body=None, *, length=None):
    """off 위치 레코드를 새 본문/length 로 바꾸고 CRC 를 다시 계산해 파일에 쓴다 (총 크기 유지 가정)."""
    raw = bytearray(path.read_bytes())
    size, old_body = record_at(raw, off)
    new = make_record(old_body if body is None else body, length=length)
    assert len(new) == size
    raw[off : off + size] = new
    path.write_bytes(bytes(raw))


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
    assert FORMAT_VERSION == 2
    assert raw == MAGIC + struct.pack("<HH", 2, 3) + b"\x00" * 8


def test_exact_record_bytes(path):
    with TableFile(path, USERS) as t:
        _, off = t.append(("홍", None))
    raw = path.read_bytes()[off:]
    body = b"\x00" + struct.pack("<q", 1) + b"\x02" + struct.pack("<I", 3) + "홍".encode()
    len_crc = struct.pack("<II", len(body), zlib.crc32(body))
    assert raw == len_crc + struct.pack("<I", zlib.crc32(len_crc)) + body


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
    bitmap = raw[off0 + RH + 9 : off0 + RH + 9 + bitmap_len]
    assert int.from_bytes(bitmap, "little") == (1 << n) - 1
    # 마지막 컬럼만 값이 있으면 그 비트만 0
    bitmap1 = raw[off1 + RH + 9 : off1 + RH + 9 + bitmap_len]
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
    # 명시적 flush 없이도 append 직후 읽힌다
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


@pytest.mark.parametrize(
    "extra",
    [
        b"\x01",
        b"\x01\x02\x03",
        b"\x00" * 11,  # 레코드 헤더(12B)에 1바이트 모자람
        make_record(b"\x00" + struct.pack("<q", 2) + b"\x03")[:-3],  # 헤더는 온전, 본문 일부만
    ],
)
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
    raw[4:6] = struct.pack("<H", 3)
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
    # header_crc 까지 다시 계산해서 "CRC 는 맞지만 length 가 최소보다 작은" 경우를 만든다 (판정 3)
    rewrite_record(path, off, length=3)
    size = path.stat().st_size
    with pytest.raises(DataCorrupted, match="minimum"):
        TableFile(path, USERS)
    assert path.stat().st_size == size


def test_non_increasing_id_is_corrupted(path):
    with TableFile(path, USERS) as t:
        t.append(("a", 1))
        _, off2 = t.append(("b", 2))
    _, body = record_at(path.read_bytes(), off2)
    rewrite_record(path, off2, body[:1] + struct.pack("<q", 1) + body[9:])  # 판정 6 (CRC 는 정상)
    with pytest.raises(DataCorrupted, match="not greater"):
        TableFile(path, USERS)


# ------------------------------------------------------------------ deleted


def test_deleted_flag_respected(path):
    with TableFile(path, USERS) as t:
        res = t.append_many([("a", 1), ("b", 2), ("c", 3)])
    off = res[1][1]
    _, body = record_at(path.read_bytes(), off)
    rewrite_record(path, off, bytes([body[0] | FLAG_DELETED]) + body[1:])  # flags 는 본문 첫 바이트
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


# ------------------------------------------------------------------ 읽기 순수성 (불변식 a, b)


class _FlushSpy:
    """쓰기 핸들을 감싸 flush 호출 횟수를 센다 (C 구현 객체라 메서드를 직접 바꿀 수 없음)."""

    def __init__(self, inner):
        self._inner = inner
        self.flush_calls = 0

    def flush(self):
        self.flush_calls += 1
        return self._inner.flush()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_append_visible_to_other_instance_without_flush(tf, path):
    # 불변식 (a): append 반환 시점에 바이트가 OS 에 전달되어 있다.
    rid, off = tf.append(("visible", 1))
    res = tf.append_many([("b1", 2), ("b2", 3)])
    with TableFile(path, USERS, readonly=True) as other:
        assert other.read_at(off) == (rid, "visible", 1)
        assert [r for _, r in other.scan()] == [(1, "visible", 1), (2, "b1", 2), (3, "b2", 3)]
        assert other.next_id == tf.next_id
    assert res[-1][1] < tf.size == path.stat().st_size


def test_append_many_flushes_once_per_batch(tf, monkeypatch):
    spy = _FlushSpy(tf._writer)
    monkeypatch.setattr(tf, "_writer", spy)
    tf.append_many([(f"r{i}", i) for i in range(100)])
    assert spy.flush_calls == 1
    tf.append(("one", 1))
    assert spy.flush_calls == 2


def test_reads_never_flush(tf, monkeypatch):
    res = tf.append_many([("a", 1), ("b", 2)])
    spy = _FlushSpy(tf._writer)
    monkeypatch.setattr(tf, "_writer", spy)
    tf.read_at(res[0][1])
    list(tf.scan())
    list(tf.records())
    assert spy.flush_calls == 0


def _state(t):
    return {k: v for k, v in vars(t).items() if k not in ("_writer", "_reader")}


def test_reads_do_not_mutate_instance(tf):
    res = tf.append_many([("a", 1), (None, None), ("c", 3)])
    before = _state(tf)
    handles = (tf._writer, tf._reader)
    for _, off in res:
        tf.read_at(off)
    assert list(tf.scan())
    assert list(tf.records())
    with pytest.raises(ValueError):
        tf.read_at(tf.size)
    assert _state(tf) == before
    assert (tf._writer, tf._reader) == handles
    assert handles[0] is tf._writer and handles[1] is tf._reader


# ------------------------------------------------------------------ v2: CRC / 비파괴 복구

from datetime import datetime  # noqa: E402

import minidb.storage as storage_mod  # noqa: E402


def three_records(path):
    with TableFile(path, USERS) as t:
        return t.append_many([("alpha", 1), ("beta", 2), ("gamma", 3)])


def dir_listing(path):
    return sorted(p.name for p in path.parent.iterdir())


def quarantine_files(path):
    return sorted(path.parent.glob(path.name + ".corrupt-*"))


def test_mid_record_length_beyond_eof_is_corrupted(path):
    res = three_records(path)
    raw = bytearray(path.read_bytes())
    off = res[1][1]
    raw[off : off + 4] = struct.pack("<I", 10_000_000)  # 파일 끝을 넘는 length (header_crc 는 그대로)
    path.write_bytes(bytes(raw))
    before = path.read_bytes()
    with pytest.raises(DataCorrupted, match="header checksum"):
        TableFile(path, USERS)
    assert path.read_bytes() == before  # 뒤의 정상 레코드를 자르지 않는다
    assert quarantine_files(path) == []


def test_mid_record_body_bitflip_is_corrupted(path):
    res = three_records(path)
    raw = bytearray(path.read_bytes())
    raw[res[1][1] + RH + 10] ^= 0x01  # 본문 1바이트 반전
    path.write_bytes(bytes(raw))
    before = path.read_bytes()
    with pytest.raises(DataCorrupted, match="body checksum"):
        TableFile(path, USERS)
    assert path.read_bytes() == before
    assert quarantine_files(path) == []


@pytest.mark.parametrize("keep", [5, RH + 4], ids=["header-partial", "body-partial"])
def test_torn_last_record_is_quarantined(path, keep):
    res = three_records(path)
    raw = path.read_bytes()
    last_off = res[2][1]
    cut = raw[last_off : last_off + keep]
    path.write_bytes(raw[: last_off + keep])

    with pytest.warns(RuntimeWarning) as rec:
        t = TableFile(path, USERS)
    msg = str(rec[0].message)
    qfiles = quarantine_files(path)
    assert len(qfiles) == 1
    assert qfiles[0].read_bytes() == cut  # 잘린 바이트와 정확히 일치
    assert t.quarantine_path == qfiles[0]
    assert f"offset {last_off}" in msg and f"{keep} trailing byte" in msg and str(qfiles[0]) in msg
    with t:
        assert path.stat().st_size == last_off
        assert [r for _, r in t.scan()] == [(1, "alpha", 1), (2, "beta", 2)]
        assert t.append(("new", 9)) == (3, last_off)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with TableFile(path, USERS) as t:
            assert [r[0] for _, r in t.scan()] == [1, 2, 3]
    assert len(quarantine_files(path)) == 1


def test_zero_filled_last_body_is_torn_tail(path):
    res = three_records(path)
    raw = bytearray(path.read_bytes())
    off = res[2][1]
    size, body = record_at(raw, off)
    raw[off + RH : off + size] = b"\x00" * len(body)  # length 는 유지, 본문만 0
    path.write_bytes(bytes(raw))
    with pytest.warns(RuntimeWarning, match="truncated"):
        with TableFile(path, USERS) as t:
            assert t.next_id == 3
            assert t.size == off
    assert quarantine_files(path)[0].read_bytes() == bytes(raw[off:])


def test_readonly_torn_tail_never_modifies(path):
    res = three_records(path)
    raw = path.read_bytes()
    path.write_bytes(raw[: res[2][1] + RH + 2])
    before_bytes = path.read_bytes()
    before_dir = dir_listing(path)
    with pytest.warns(RuntimeWarning, match="readonly"):
        with TableFile(path, USERS, readonly=True) as t:
            assert t.size == res[2][1]
            assert [r for _, r in t.scan()] == [(1, "alpha", 1), (2, "beta", 2)]
    assert path.read_bytes() == before_bytes
    assert dir_listing(path) == before_dir


def test_version1_file_rejected(path):
    TableFile(path, USERS).close()
    raw = bytearray(path.read_bytes())
    raw[4:6] = struct.pack("<H", 1)
    path.write_bytes(bytes(raw))
    with pytest.raises(DataCorrupted, match=r"unsupported storage format version 1 \(expected 2\)"):
        TableFile(path, USERS)


def test_bitflip_after_open_detected_on_read(path):
    with TableFile(path, USERS) as t:
        res = t.append_many([("alpha", 1), ("beta", 2), ("gamma", 3)])
        off = res[1][1]
        with open(path, "r+b") as other:  # 열린 뒤 디스크에서 비트가 바뀐 상황
            other.seek(off + RH + 9 + 1 + 4)  # name 텍스트 첫 바이트
            b = other.read(1)
            other.seek(-1, 1)
            other.write(bytes([b[0] ^ 0x20]))
        with pytest.raises(DataCorrupted, match="body checksum"):
            t.read_at(off)
        assert t.read_at(res[0][1]) == (1, "alpha", 1)
        with pytest.raises(DataCorrupted, match="body checksum"):
            list(t.scan())


def test_header_bitflip_after_open_detected_on_read(path):
    with TableFile(path, USERS) as t:
        res = t.append_many([("alpha", 1), ("beta", 2)])
        with open(path, "r+b") as other:
            other.seek(res[0][1])
            other.write(b"\xff")
        with pytest.raises(DataCorrupted):
            t.read_at(res[0][1])
        with pytest.raises(DataCorrupted, match="header checksum"):
            list(t.scan())


def test_quarantine_name_collision(path, monkeypatch):
    fixed = datetime(2026, 11, 12, 9, 30, 15)
    monkeypatch.setattr(storage_mod, "_now", lambda: fixed)
    res = three_records(path)
    base = path.name + ".corrupt-20261112T093015"
    (path.parent / base).write_bytes(b"old")
    (path.parent / (base + "-1")).write_bytes(b"old1")
    path.write_bytes(path.read_bytes()[: res[2][1] + 3])
    with pytest.warns(RuntimeWarning, match=base + "-2"):
        TableFile(path, USERS).close()
    assert (path.parent / base).read_bytes() == b"old"  # 기존 파일은 덮어쓰지 않는다
    assert (path.parent / (base + "-1")).read_bytes() == b"old1"
    assert (path.parent / (base + "-2")).stat().st_size == 3


# ------------------------------------------------------------------ 판정 0: 0 으로 채워진 꼬리


@pytest.mark.parametrize("nzeros", [12, 100, 4096])
def test_zero_filled_tail_is_quarantined(path, nzeros):
    three_records(path)
    good = path.stat().st_size
    with open(path, "ab") as f:
        f.write(b"\x00" * nzeros)
    with pytest.warns(RuntimeWarning, match=f"truncated {nzeros} trailing byte"):
        t = TableFile(path, USERS)
    with t:
        assert t.size == good == path.stat().st_size
        assert t.next_id == 4
        assert t.append(("after", 4)) == (4, good)
    q = quarantine_files(path)
    assert len(q) == 1 and q[0].read_bytes() == b"\x00" * nzeros
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        TableFile(path, USERS).close()


def test_zero_filled_data_region_on_empty_table(path):
    TableFile(path, USERS).close()
    with open(path, "ab") as f:
        f.write(b"\x00" * 50)
    with pytest.warns(RuntimeWarning):
        with TableFile(path, USERS) as t:
            assert t.size == HEADER_SIZE and t.next_id == 1


@pytest.mark.parametrize("where", [0, 20, 99], ids=["first", "middle", "last"])
def test_nonzero_byte_among_zero_tail_is_corrupted(path, where):
    three_records(path)
    tail = bytearray(100)
    tail[where] = 0x01
    with open(path, "ab") as f:
        f.write(bytes(tail))
    before = path.read_bytes()
    with pytest.raises(DataCorrupted, match="header checksum"):
        TableFile(path, USERS)
    assert path.read_bytes() == before
    assert quarantine_files(path) == []


def test_zero_filled_tail_readonly_never_modifies(path):
    three_records(path)
    good = path.stat().st_size
    with open(path, "ab") as f:
        f.write(b"\x00" * 100)
    before_bytes = path.read_bytes()
    before_dir = dir_listing(path)
    with pytest.warns(RuntimeWarning, match="readonly"):
        with TableFile(path, USERS, readonly=True) as t:
            assert t.size == good
            assert [r[0] for _, r in t.scan()] == [1, 2, 3]
    assert path.read_bytes() == before_bytes
    assert dir_listing(path) == before_dir


def test_trailing_zero_bytes_inside_last_record_are_not_tail(path):
    # 마지막 레코드가 0 바이트로 끝나도(age=0 의 상위 바이트 등) 정상 레코드로 인정해야 한다
    with TableFile(path, USERS) as t:
        t.append((None, 0))
    raw = path.read_bytes()
    assert raw.endswith(b"\x00" * 8)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with TableFile(path, USERS) as t:
            assert [r for _, r in t.scan()] == [(1, None, 0)]
