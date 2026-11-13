"""B+ 트리 단위 테스트: 분할 경계, 무작위 삽입, 범위, 벌크 로딩, 분할 정책."""

import random
from bisect import bisect_left, bisect_right

import pytest

from minidb.index import BPlusTree, DuplicateKeyError
from minidb.index.bptree import DEFAULT_ORDER, _Internal, _Leaf

ORDERS = [3, 4, 5, 16]


def build(keys, order, **kw):
    t = BPlusTree(order, **kw)
    for k in keys:
        t.insert(k, k * 10)
    return t


def bulk(keys, order, fill=0.9):
    b = BPlusTree.bulk_builder(order, fill)
    for k in keys:
        b.add(k, k * 10)
    return b.finish()


def expected_range(sorted_keys, lo, hi, lo_inc, hi_inc):
    i = 0 if lo is None else (bisect_left(sorted_keys, lo) if lo_inc else bisect_right(sorted_keys, lo))
    j = len(sorted_keys) if hi is None else (bisect_right(sorted_keys, hi) if hi_inc else bisect_left(sorted_keys, hi))
    return [(k, k * 10) for k in sorted_keys[i:j]]


def assert_matches(t, keys):
    """search / items / range / len 을 정렬된 리스트와 대조한다."""
    s = sorted(keys)
    assert len(t) == len(s)
    assert list(t.items()) == [(k, k * 10) for k in s]
    for k in s:
        assert t.search(k) == k * 10
    present = set(s)
    lo_k, hi_k = (s[0] - 2, s[-1] + 2) if s else (-2, 2)
    for k in range(lo_k, hi_k + 1):
        if k not in present:
            assert t.search(k) is None


# ------------------------------------------------------------------ 기본


def test_empty_tree():
    t = BPlusTree(4)
    assert len(t) == 0
    assert t.height == 1
    assert t.search(1) is None
    assert list(t.items()) == []
    assert list(t.range(1, 10)) == []
    t.check_invariants()
    st = t.stats()
    assert (st["nodes"], st["leaves"], st["internal_nodes"], st["height"]) == (1, 1, 0, 1)


def test_single_key():
    t = BPlusTree(4)
    t.insert(7, 70)
    t.check_invariants()
    assert t.search(7) == 70 and t.search(6) is None and 7 in t and 8 not in t
    assert list(t.items()) == [(7, 70)]
    assert len(t) == 1 and t.height == 1


def test_default_order():
    assert BPlusTree().order == DEFAULT_ORDER


@pytest.mark.parametrize("bad", [0, 1, 2, -5, 3.0, "4"])
def test_invalid_order(bad):
    with pytest.raises(ValueError):
        BPlusTree(bad)


@pytest.mark.parametrize("fill", [0.4, 1.01])
def test_invalid_fill_factor(fill):
    with pytest.raises(ValueError):
        BPlusTree(4, leaf_fill_factor=fill)
    with pytest.raises(ValueError):
        BPlusTree.bulk_builder(4, fill)


def test_key_out_of_int64_range_leaves_tree_unchanged():
    t = build(range(10), 4)
    with pytest.raises(OverflowError):
        t.insert(2**63, 1)
    with pytest.raises(OverflowError):
        t.insert(100, 2**63)
    t.check_invariants()
    assert_matches(t, range(10))
    assert t.search(2**70) is None  # 조회는 범위 밖 키도 그냥 None


# ------------------------------------------------------------------ 분할 경계


@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize("rightmost", [True, False])
def test_leaf_split_boundary(order, rightmost):
    """order 개까지는 루트 리프 하나, order+1 번째에 처음 분할된다."""
    t = BPlusTree(order, rightmost_split=rightmost)
    for k in range(order):
        t.insert(k, k * 10)
    t.check_invariants()
    assert t.height == 1 and t.stats()["leaves"] == 1
    t.insert(order, order * 10)
    t.check_invariants()
    assert t.height == 2 and t.stats()["leaves"] == 2
    assert_matches(t, range(order + 1))


@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize("rightmost", [True, False])
def test_root_internal_split_boundary(order, rightmost):
    """높이가 3 으로 늘어나기 직전과 직후 모두 불변식을 만족한다."""
    t = BPlusTree(order, rightmost_split=rightmost)
    k = 0
    while t.height < 3:
        before = t.height
        t.insert(k, k * 10)
        t.check_invariants()
        k += 1
        if t.height == 3:
            assert before == 2
            root = t._root
            assert isinstance(root, _Internal) and len(root.keys) == 1  # 새 루트는 키 하나
    assert_matches(t, range(k))


@pytest.mark.parametrize("order", ORDERS)
def test_descending_and_middle_inserts(order):
    keys = list(range(200, 0, -1))
    t = build(keys, order)
    t.check_invariants()
    assert_matches(t, keys)
    # 기존 키 사이 (홀수만 있는 트리에 짝수 끼워 넣기)
    t = build(range(1, 301, 2), order)
    for k in range(2, 301, 2):
        t.insert(k, k * 10)
        t.check_invariants()
    assert_matches(t, range(1, 301))


# ------------------------------------------------------------------ 무작위


@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize("rightmost", [True, False])
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_random_small_checked_every_insert(order, rightmost, seed):
    rng = random.Random(seed * 100 + order)
    keys = rng.sample(range(-500, 2000), 400)
    t = BPlusTree(order, rightmost_split=rightmost)
    for k in keys:
        t.insert(k, k * 10)
        t.check_invariants()
    assert_matches(t, keys)


@pytest.mark.parametrize("order", [4, 64])
def test_random_100k(order):
    rng = random.Random(order)
    keys = rng.sample(range(1, 10_000_000), 100_000)
    t = build(keys, order)
    t.check_invariants()
    s = sorted(keys)
    assert list(t.items()) == [(k, k * 10) for k in s]
    for k in rng.sample(keys, 2000):
        assert t.search(k) == k * 10
    for _ in range(100):
        lo = rng.randrange(0, 10_000_000)
        hi = lo + rng.choice([0, 10, 1000, 100_000])
        lo_inc, hi_inc = rng.random() < 0.5, rng.random() < 0.5
        assert list(t.range(lo, hi, lo_inc, hi_inc)) == expected_range(s, lo, hi, lo_inc, hi_inc)


def test_mixed_sequential_then_random():
    """맨 오른쪽 최적화로 덜 찬 맨 오른쪽 노드가 있는 상태에서 무작위 삽입이 섞여도 불변식 유지."""
    rng = random.Random(5)
    t = BPlusTree(5)
    keys = list(range(0, 3000, 3))
    for k in keys:
        t.insert(k, k * 10)
    t.check_invariants()
    extra = rng.sample([k for k in range(0, 3100) if k % 3], 800)
    for k in extra:
        t.insert(k, k * 10)
        t.check_invariants()
    assert_matches(t, keys + extra)


# ------------------------------------------------------------------ 중복


@pytest.mark.parametrize("order", ORDERS)
def test_duplicate_rejected(order):
    t = build(range(50), order)
    for k in (0, 25, 49):
        with pytest.raises(DuplicateKeyError) as ei:
            t.insert(k, -1)
        assert isinstance(ei.value, KeyError)
        assert ei.value.key == k
        assert t.search(k) == k * 10  # 기존 값 유지
    assert len(t) == 50
    t.check_invariants()


# ------------------------------------------------------------------ 범위


@pytest.fixture(scope="module")
def tree100():
    # order 4 → 리프가 많아 범위가 리프 경계를 여러 번 넘는다
    return build(range(0, 200, 2), 4)


KEYS100 = list(range(0, 200, 2))


@pytest.mark.parametrize("lo_inc", [True, False])
@pytest.mark.parametrize("hi_inc", [True, False])
@pytest.mark.parametrize(("lo", "hi"), [(10, 50), (11, 49), (10, 11), (0, 198), (-5, 500), (50, 50), (51, 51)])
def test_range_inclusive_combinations(tree100, lo, hi, lo_inc, hi_inc):
    assert list(tree100.range(lo, hi, lo_inc, hi_inc)) == expected_range(KEYS100, lo, hi, lo_inc, hi_inc)


def test_range_endpoints_inclusion(tree100):
    keys = lambda **kw: [k for k, _ in tree100.range(**kw)]  # noqa: E731
    assert keys(lo=10, hi=16) == [10, 12, 14, 16]
    assert keys(lo=10, hi=16, lo_inclusive=False) == [12, 14, 16]
    assert keys(lo=10, hi=16, hi_inclusive=False) == [10, 12, 14]
    assert keys(lo=10, hi=16, lo_inclusive=False, hi_inclusive=False) == [12, 14]


def test_range_unbounded(tree100):
    assert [k for k, _ in tree100.range(hi=6)] == [0, 2, 4, 6]
    assert [k for k, _ in tree100.range(lo=192)] == [192, 194, 196, 198]
    assert [k for k, _ in tree100.range(hi=6, hi_inclusive=False)] == [0, 2, 4]
    assert [k for k, _ in tree100.range(lo=192, lo_inclusive=False)] == [194, 196, 198]
    assert list(tree100.range()) == [(k, k * 10) for k in KEYS100]


@pytest.mark.parametrize(
    ("lo", "hi", "lo_inc", "hi_inc"),
    [(50, 40, True, True), (50, 50, False, True), (50, 50, True, False), (51, 51, True, True), (1000, None, True, True),
     (None, -1, True, True), (2**70, None, True, True), (None, -(2**70), True, True)],
)  # fmt: skip
def test_range_empty(tree100, lo, hi, lo_inc, hi_inc):
    assert list(tree100.range(lo, hi, lo_inc, hi_inc)) == []


def test_range_crosses_leaf_boundaries(tree100):
    # 모든 리프 경계에서 시작/끝나는 범위
    leaf = tree100._head
    firsts = []
    while leaf is not None:
        firsts.append(leaf.keys[0])
        leaf = leaf.next
    assert len(firsts) > 10
    for a, b in zip(firsts, firsts[2:]):
        assert list(tree100.range(a, b)) == expected_range(KEYS100, a, b, True, True)
        assert list(tree100.range(a - 1, b - 1, False, False)) == expected_range(KEYS100, a - 1, b - 1, False, False)


def test_range_is_lazy(tree100):
    it = tree100.range(0)
    assert next(it) == (0, 0)
    assert next(it) == (2, 20)
    it.close()


# ------------------------------------------------------------------ 벌크 로딩


@pytest.mark.parametrize("order", ORDERS + [64])
@pytest.mark.parametrize("fill", [0.5, 0.7, 0.9, 1.0])
def test_bulk_sizes(order, fill):
    for n in sorted({0, 1, order - 1, order, order + 1, 2 * order, 2 * order + 1, 1000, 12_345}):
        keys = [k * 3 + 1 for k in range(n)]
        t = bulk(keys, order, fill)
        t.check_invariants()
        assert list(t.items()) == list(build(keys, order).items())
        assert_matches(t, keys)


@pytest.mark.parametrize("order", ORDERS)
def test_bulk_every_size_up_to_limit(order):
    """마지막 노드 재분배/병합이 모든 나머지 크기에서 올바른지: 0~400 전부."""
    for n in range(0, 400):
        t = bulk(range(n), order)
        t.check_invariants()
        assert len(t) == n


def test_bulk_then_insert():
    t = bulk(range(0, 2000, 2), 8)
    for k in range(1, 2000, 2):
        t.insert(k, k * 10)
    for k in range(2000, 2500):
        t.insert(k, k * 10)
    t.check_invariants()
    assert_matches(t, range(2500))


@pytest.mark.parametrize("bad", [[1, 1], [2, 1], [1, 5, 3]])
def test_bulk_rejects_non_increasing(bad):
    b = BPlusTree.bulk_builder(4)
    with pytest.raises(ValueError, match="strictly increasing"):
        for k in bad:
            b.add(k, 0)


def test_bulk_builder_single_use():
    b = BPlusTree.bulk_builder(4)
    b.add(1, 1)
    b.finish()
    with pytest.raises(RuntimeError):
        b.add(2, 2)
    with pytest.raises(RuntimeError):
        b.finish()


def test_bulk_fill_factor_respected():
    t = bulk(range(100_000), 64, 0.9)
    st = t.stats()
    assert abs(st["leaf_fill_avg"] - int(64 * 0.9) / 64) < 0.01
    assert st["leaf_fill_min"] >= 0.5


# ------------------------------------------------------------------ 분할 정책


def test_sequential_fill_rightmost_on_vs_off():
    on = build(range(100_000), 64, rightmost_split=True)
    off = build(range(100_000), 64, rightmost_split=False)
    on.check_invariants()
    off.check_invariants()
    on_fill = on.stats()["leaf_fill_avg"]
    off_fill = off.stats()["leaf_fill_avg"]
    assert on_fill >= 0.88, on_fill
    assert 0.48 <= off_fill <= 0.55, off_fill
    assert on.stats()["leaves"] < off.stats()["leaves"] * 0.6
    assert list(on.items()) == list(off.items())


def test_rightmost_split_only_for_appends():
    """맨 오른쪽 리프라도 최댓값보다 작은 키가 들어와 넘치면 반반 분할."""
    t = BPlusTree(10)
    for k in range(0, 20, 2):  # 10 keys, 루트 리프 가득
        t.insert(k, k)
    t.insert(5, 5)  # 중간 삽입으로 분할
    leaves = [len(x.keys) for x in t._levels()[-1]]
    assert leaves == [6, 5]
    t2 = BPlusTree(10)
    for k in range(11):  # 마지막이 최댓값 → 9:2
        t2.insert(k, k)
    assert [len(x.keys) for x in t2._levels()[-1]] == [9, 2]


def test_check_invariants_detects_corruption():
    t = build(range(100), 4)
    leaf = t._head.next
    leaf.keys[0], leaf.keys[1] = leaf.keys[1], leaf.keys[0]
    with pytest.raises(AssertionError):
        t.check_invariants()

    t = build(range(100), 4)
    t._size += 1
    with pytest.raises(AssertionError, match="len"):
        t.check_invariants()

    t = build(range(100), 4)
    t._head.next = t._head.next.next  # 리프 하나를 연결 리스트에서 빠뜨림
    with pytest.raises(AssertionError, match="chain"):
        t.check_invariants()

    t = build(range(100), 4)
    root = t._root
    root.keys[0] += 1  # 구분 키와 자식 범위 불일치
    with pytest.raises(AssertionError, match="bound"):
        t.check_invariants()


def test_strict_minimum_when_rightmost_off():
    """최적화를 끈 트리에서 덜 찬 노드는 맨 오른쪽이라도 위반이다."""
    t = BPlusTree(8, rightmost_split=False)
    for k in range(40):
        t.insert(k, k)
    t.check_invariants()
    last = t._levels()[-1][-1]
    assert isinstance(last, _Leaf)
    del last.keys[1:]
    del last.values[1:]
    t._size = sum(len(x.keys) for x in t._levels()[-1])
    with pytest.raises(AssertionError, match="minimum"):
        t.check_invariants()


def test_stats_fields():
    t = build(range(1000), 16)
    st = t.stats()
    assert st["keys"] == 1000 and st["order"] == 16 and st["height"] == t.height
    assert st["nodes"] == st["leaves"] + st["internal_nodes"]
    assert 0 < st["leaf_fill_min"] <= st["leaf_fill_avg"] <= 1
