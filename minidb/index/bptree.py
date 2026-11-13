"""메모리 기반 B+ 트리: 정수 키(id) → 정수 값(레코드 offset).

구조 (자세한 그림과 설명은 docs/bptree.md)::

                       [Internal: keys=[40, 80]]
                      /           |            \\
          [Leaf 10 20 30] → [Leaf 40 50 60] → [Leaf 80 90] → None
           values ...          values ...       values ...

- **리프**: ``keys`` / ``values`` 를 ``array('q')`` (8바이트 부호 있는 정수 배열)로, 다음 리프 참조 ``next`` 를 가진다.
  모든 키-값은 리프에만 있고 리프들은 키 순서대로 연결 리스트를 이룬다 → 범위 검색은 시작 리프를 찾은 뒤 옆으로 걷는다.
- **내부 노드**: ``keys`` (``array('q')``)와 ``children`` (list). ``len(children) == len(keys) + 1``.
  ``children[i]`` 서브트리의 키 k 는 ``keys[i-1] <= k < keys[i]`` 를 만족한다 (양 끝은 무한).
  그래서 자식 선택은 ``bisect_right(keys, k)`` 한 번이다.
- **order** = 노드 하나의 최대 키 개수 (리프·내부 공통). 루트가 아닌 노드의 최소 키 개수는
  리프 ``ceil(order/2)``, 내부 ``floor(order/2)`` (반반 분할의 결과가 항상 이 범위에 든다).

분할 정책:

- 일반 분할: 넘친 노드(order+1 개)를 반반 나눈다.
- **맨 오른쪽 분할 최적화** (``rightmost_split=True``, 기본): 맨 오른쪽 리프(다음 리프 없음)에 그 리프의 최댓값보다
  큰 키가 들어와 넘치면, 왼쪽 노드를 ``leaf_fill_factor`` (기본 0.9) 만큼 채우고 나머지만 오른쪽으로 넘긴다.
  자동 증가 id 처럼 키가 항상 커지는 삽입에서 반반 분할을 하면 왼쪽 노드는 다시는 키를 받지 않으므로 채움률이 50% 에
  머문다. PostgreSQL B-tree 도 맨 오른쪽 리프 페이지를 분할할 때 fillfactor(기본 90%)만큼 왼쪽을 채운다.
  같은 원리를 맨 오른쪽 경로의 내부 노드 분할에도 적용한다.
  이 정책에서는 맨 오른쪽 경로(right spine)의 노드가 최소 키 개수보다 적을 수 있다 (곧 채워질 노드이므로 허용).
  :meth:`BPlusTree.check_invariants` 는 이 예외를 이 경로의 노드에만, 최적화가 켜진 트리에서만 허용한다.

**스레드 안전하지 않다.** 이번 단계에서는 한 스레드에서만 사용한다. GIL 의 원자성에도 기대지 않는다
(3.14t 에는 GIL 이 없고, 삽입은 여러 노드를 차례로 고치는 다단계 작업이다).
Phase 3 락 계획: :meth:`search` / :meth:`range` / :meth:`items` 는 공유(read) 락, :meth:`insert` 는 배타(write) 락.
``range`` 는 지연 제너레이터이므로 **순회가 끝날 때까지** 공유 락을 쥐고 있어야 한다 (순회 중 분할이 일어나면
리프 연결이 바뀐다). 표시: ``# [LOCK:read]`` / ``# [LOCK:write]``.
"""

from __future__ import annotations

from array import array
from bisect import bisect_left, bisect_right
from collections.abc import Iterator
from typing import Any

__all__ = ["BPlusTree", "BulkBuilder", "DuplicateKeyError", "DEFAULT_ORDER", "DEFAULT_LEAF_FILL_FACTOR"]

#: 기본 order (노드당 최대 키 수). scripts/bench_index.py 의 order 비교 결과로 정했다 (docs/phase2-report.md).
DEFAULT_ORDER: int = 128
#: 맨 오른쪽 분할과 벌크 로딩의 채움률. PostgreSQL B-tree 리프 fillfactor 기본값(90)과 같다.
DEFAULT_LEAF_FILL_FACTOR: float = 0.9
MIN_ORDER: int = 3


class DuplicateKeyError(KeyError):
    """이미 있는 키를 삽입하려 할 때. ``key`` 속성에 키가 있다."""

    def __init__(self, key: int) -> None:
        super().__init__(key)
        self.key = key

    def __str__(self) -> str:
        return f"duplicate key {self.key}"


class _Leaf:
    __slots__ = ("keys", "values", "next")

    def __init__(self, keys: array, values: array, next_leaf: _Leaf | None = None) -> None:
        self.keys = keys
        self.values = values
        self.next = next_leaf


class _Internal:
    __slots__ = ("keys", "children")

    def __init__(self, keys: array, children: list[Any]) -> None:
        self.keys = keys
        self.children = children


def _check_params(order: int, leaf_fill_factor: float) -> None:
    if type(order) is not int or order < MIN_ORDER:
        raise ValueError(f"order must be an int >= {MIN_ORDER}, got {order!r}")
    if not 0.5 <= leaf_fill_factor <= 1.0:
        raise ValueError(f"leaf_fill_factor must be in [0.5, 1.0], got {leaf_fill_factor!r}")


def _leaf_fill_target(order: int, fill: float) -> int:
    """맨 오른쪽 분할/벌크 로딩에서 왼쪽 리프에 남길 키 수: order*fill 을 [최소, order] 로 자른 값."""
    return max((order + 1) // 2, min(order, int(order * fill)))


def _internal_fill_target(order: int, fill: float) -> int:
    """내부 노드 버전. 오른쪽 노드가 키를 최소 1개 갖도록 상한은 order-1 (분할 시 하나는 위로 올라간다)."""
    return max(order // 2, min(order - 1, int(order * fill)))


class BPlusTree:
    """정수 키 → 정수 값 B+ 트리 (키 유일). 모듈 docstring 참고.

    Args:
        order: 노드당 최대 키 개수 (>= 3).
        leaf_fill_factor: 맨 오른쪽 분할 때 왼쪽 노드를 채울 비율 (0.5 ~ 1.0).
        rightmost_split: False 면 맨 오른쪽 분할 최적화를 끄고 항상 반반 분할한다 (비교 실험용).

    키와 값은 64비트 부호 있는 정수 범위여야 한다 (``array('q')``). 범위 밖이면 OverflowError.
    """

    __slots__ = ("order", "leaf_fill_factor", "rightmost_split", "_root", "_head", "_size", "_height")

    def __init__(
        self,
        order: int = DEFAULT_ORDER,
        *,
        leaf_fill_factor: float = DEFAULT_LEAF_FILL_FACTOR,
        rightmost_split: bool = True,
    ) -> None:
        _check_params(order, leaf_fill_factor)
        self.order = order
        self.leaf_fill_factor = leaf_fill_factor
        self.rightmost_split = rightmost_split
        leaf = _Leaf(array("q"), array("q"))
        # [LOCK:write] 아래 네 속성은 insert 가 바꾸는 공유 상태.
        self._root: _Leaf | _Internal = leaf
        self._head: _Leaf = leaf  # 가장 왼쪽 리프 (삽입만 있으므로 바뀌지 않는다: 분할은 항상 오른쪽 노드를 새로 만든다)
        self._size = 0
        self._height = 1

    # ------------------------------------------------------------ 생성
    @classmethod
    def bulk_builder(
        cls, order: int = DEFAULT_ORDER, leaf_fill_factor: float = DEFAULT_LEAF_FILL_FACTOR
    ) -> BulkBuilder:
        """정렬된 키를 차례로 받아 트리를 아래에서부터 쌓는 빌더 (:class:`BulkBuilder`)."""
        return BulkBuilder(order, leaf_fill_factor)

    # ------------------------------------------------------------ 조회
    def __len__(self) -> int:
        return self._size

    @property
    def height(self) -> int:
        """루트에서 리프까지의 레벨 수 (리프 하나뿐이면 1)."""
        return self._height

    def _find_leaf(self, key: int) -> _Leaf:
        node = self._root
        while type(node) is _Internal:
            node = node.children[bisect_right(node.keys, key)]
        return node  # type: ignore[return-value]

    def search(self, key: int) -> int | None:
        """키의 값을 반환한다 (없으면 None). O(log n)."""
        # [LOCK:read]
        node = self._root
        while type(node) is _Internal:
            node = node.children[bisect_right(node.keys, key)]
        keys = node.keys
        i = bisect_left(keys, key)
        if i < len(keys) and keys[i] == key:
            return node.values[i]  # type: ignore[union-attr]
        return None

    def __contains__(self, key: object) -> bool:
        return type(key) is int and self.search(key) is not None

    def range(
        self,
        lo: int | None = None,
        hi: int | None = None,
        lo_inclusive: bool = True,
        hi_inclusive: bool = True,
    ) -> Iterator[tuple[int, int]]:
        """``lo``~``hi`` 범위의 ``(key, value)`` 를 키 순서대로 내보내는 제너레이터.

        시작 리프를 루트에서 한 번 찾은 뒤 리프 연결 리스트를 따라간다. ``lo``/``hi`` 가 None 이면 그쪽은 무한.
        지연 평가라서 소비자가 멈추면(LIMIT) 더 이상 리프를 읽지 않는다.
        """
        # [LOCK:read] 순회가 끝날 때까지 공유 락 유지 (제너레이터).
        if lo is None:
            leaf: _Leaf | None = self._head
            i = 0
        else:
            leaf = self._find_leaf(lo)
            i = bisect_left(leaf.keys, lo) if lo_inclusive else bisect_right(leaf.keys, lo)
        while leaf is not None:
            keys = leaf.keys
            values = leaf.values
            if hi is None:
                j = len(keys)
            else:
                j = bisect_right(keys, hi) if hi_inclusive else bisect_left(keys, hi)
            for k in range(i, j):
                yield keys[k], values[k]
            if j < len(keys):
                return  # 이 리프 안에서 상한을 넘었다
            leaf = leaf.next
            i = 0

    def items(self) -> Iterator[tuple[int, int]]:
        """모든 ``(key, value)`` 를 키 순서대로."""
        return self.range()

    def keys(self) -> Iterator[int]:
        """모든 키를 순서대로."""
        return (k for k, _ in self.range())

    # ------------------------------------------------------------ 삽입
    def insert(self, key: int, value: int) -> None:
        """키-값을 넣는다. 이미 있는 키면 :class:`DuplicateKeyError` (트리는 바뀌지 않는다).

        1. 루트에서 리프까지 내려가며 지나온 (내부 노드, 자식 번호)를 기록한다.
        2. 리프에 정렬 위치로 넣는다. order 이하면 끝.
        3. 넘치면 리프를 둘로 나누고, 오른쪽 리프의 첫 키를 구분 키로 부모에 넣는다.
        4. 부모도 넘치면 가운데 키를 위로 올리며 나눈다. 루트까지 넘치면 새 루트를 만들어 높이가 1 늘어난다.
        """
        # [LOCK:write] 내려가기부터 분할 전파까지 전체가 임계 구역.
        order = self.order
        path: list[tuple[_Internal, int]] = []
        node = self._root
        while type(node) is _Internal:
            ci = bisect_right(node.keys, key)
            path.append((node, ci))
            node = node.children[ci]
        leaf: _Leaf = node  # type: ignore[assignment]
        keys = leaf.keys
        i = bisect_left(keys, key)
        if i < len(keys) and keys[i] == key:
            raise DuplicateKeyError(key)
        keys.insert(i, key)  # 키/값 범위 오류(OverflowError)면 여기서 실패 → 아무것도 안 바뀜
        try:
            leaf.values.insert(i, value)
        except BaseException:
            del keys[i]
            raise
        self._size += 1
        if len(keys) <= order:
            return

        # ---- 리프 분할
        # 맨 오른쪽 리프 = 다음 리프가 없음. 이 리프에 이르는 경로는 매 단계 마지막 자식이었으므로
        # 경로 위의 내부 노드도 모두 맨 오른쪽 경로(right spine)에 있다.
        on_spine = self.rightmost_split and leaf.next is None
        if on_spine and i == len(keys) - 1:
            cut = _leaf_fill_target(order, self.leaf_fill_factor)  # 순차 삽입: 왼쪽을 채움률만큼
        else:
            cut = (len(keys) + 1) // 2  # 일반: 반반 (왼쪽이 하나 더 많거나 같음)
        right = _Leaf(keys[cut:], leaf.values[cut:], leaf.next)
        del keys[cut:]
        del leaf.values[cut:]
        leaf.next = right
        sep = right.keys[0]
        new_node: _Leaf | _Internal = right

        # ---- 상위 전파
        while path:
            parent, ci = path.pop()
            pkeys = parent.keys
            pkeys.insert(ci, sep)
            parent.children.insert(ci + 1, new_node)
            if len(pkeys) <= order:
                return
            if on_spine and ci == len(pkeys) - 1:
                mid = _internal_fill_target(order, self.leaf_fill_factor)
            else:
                mid = len(pkeys) // 2
            sep = pkeys[mid]  # 가운데 키는 위로 올라가고 어느 쪽에도 남지 않는다
            new_node = _Internal(pkeys[mid + 1 :], parent.children[mid + 1 :])
            del pkeys[mid:]
            del parent.children[mid + 1 :]

        # 루트가 분할됨 → 새 루트
        self._root = _Internal(array("q", (sep,)), [self._root, new_node])
        self._height += 1

    # ------------------------------------------------------------ 통계 / 검사
    def _levels(self) -> list[list[_Leaf | _Internal]]:
        levels: list[list[_Leaf | _Internal]] = [[self._root]]
        while type(levels[-1][0]) is _Internal:
            levels.append([c for n in levels[-1] for c in n.children])  # type: ignore[union-attr]
        return levels

    def stats(self) -> dict[str, Any]:
        """노드 수, 리프 수, 높이, 채움률(키 수 / order) 통계.

        ``leaf_fill_min`` / ``internal_fill_min`` 은 루트를 제외한 노드 중 최솟값 (노드가 루트뿐이면 None).
        """
        # [LOCK:read]
        levels = self._levels()
        leaves = levels[-1]
        internals = [n for lvl in levels[:-1] for n in lvl]
        order = self.order
        root = self._root

        def fills(nodes: list[Any]) -> list[float]:
            return [len(n.keys) / order for n in nodes if n is not root]

        leaf_fills = fills(leaves)
        internal_fills = fills(internals)
        all_leaf = [len(n.keys) / order for n in leaves]
        all_internal = [len(n.keys) / order for n in internals]
        return {
            "keys": self._size,
            "order": order,
            "height": self._height,
            "nodes": len(leaves) + len(internals),
            "leaves": len(leaves),
            "internal_nodes": len(internals),
            "leaf_fill_avg": sum(all_leaf) / len(all_leaf),
            "leaf_fill_min": min(leaf_fills) if leaf_fills else None,
            "internal_fill_avg": sum(all_internal) / len(all_internal) if all_internal else None,
            "internal_fill_min": min(internal_fills) if internal_fills else None,
        }

    def check_invariants(self) -> None:
        """구조 불변식을 모두 검사한다. 위반이면 AssertionError.

        1. 모든 리프의 깊이가 같다 (= height).
        2. 루트가 아닌 노드의 키 수: 리프 [ceil(order/2), order], 내부 [floor(order/2), order].
           (맨 오른쪽 분할 최적화가 켜진 트리에서는 맨 오른쪽 경로의 노드만 하한 대신 1 이상)
           루트: 리프면 [0, order], 내부면 [1, order]. 내부 노드는 자식이 키보다 하나 많다.
        3. 노드 안 키는 엄격히 오름차순.
        4. 내부 노드의 구분 키와 자식 서브트리의 키 범위가 맞는다: children[i] 의 모든 키 k 는 keys[i-1] <= k < keys[i].
        5. 리프 연결 리스트가 모든 키를 오름차순으로 정확히 한 번씩 포함하고, 트리의 리프 순서와 같다.
        6. ``len(tree)`` 와 실제 키 수가 같다.
        """
        order = self.order
        min_leaf = (order + 1) // 2
        min_internal = order // 2
        leaves_in_order: list[_Leaf] = []

        def visit(node: Any, depth: int, lo: int | None, hi: int | None, is_root: bool, on_spine: bool) -> int:
            keys = node.keys
            n = len(keys)
            assert isinstance(keys, array) and keys.typecode == "q", "keys must be array('q')"
            assert n <= order, f"node has {n} keys > order {order}"
            for a, b in zip(keys, keys[1:]):
                assert a < b, f"keys not strictly increasing: {a} >= {b}"
            if n:
                assert lo is None or keys[0] >= lo, f"key {keys[0]} < lower bound {lo}"
                assert hi is None or keys[-1] < hi, f"key {keys[-1]} >= upper bound {hi}"
            relaxed = self.rightmost_split and on_spine
            if type(node) is _Leaf:
                assert depth == self._height, f"leaf at depth {depth}, expected {self._height}"
                assert isinstance(node.values, array) and node.values.typecode == "q"
                assert len(node.values) == n, "keys/values length mismatch"
                if not is_root:
                    lower = 1 if relaxed else min_leaf
                    assert n >= lower, f"leaf has {n} keys < minimum {lower}"
                leaves_in_order.append(node)
                return n
            assert type(node) is _Internal, f"unexpected node type {type(node).__name__}"
            assert len(node.children) == n + 1, "internal node must have len(keys)+1 children"
            lower = 1 if (is_root or relaxed) else min_internal
            assert n >= lower, f"internal node has {n} keys < minimum {lower}"
            total = 0
            last = len(node.children) - 1
            for i, child in enumerate(node.children):
                c_lo = lo if i == 0 else keys[i - 1]
                c_hi = hi if i == last else keys[i]
                total += visit(child, depth + 1, c_lo, c_hi, False, on_spine and i == last)
            return total

        counted = visit(self._root, 1, None, None, True, True)
        assert counted == self._size, f"len() is {self._size} but tree holds {counted} keys"

        # 리프 연결 리스트
        assert self._head is leaves_in_order[0], "head is not the leftmost leaf"
        chain: list[_Leaf] = []
        leaf: _Leaf | None = self._head
        prev: int | None = None
        seen = 0
        while leaf is not None:
            assert len(chain) < len(leaves_in_order), "leaf chain is longer than the tree (cycle?)"
            chain.append(leaf)
            for k in leaf.keys:
                assert prev is None or k > prev, f"leaf chain not ascending at {k}"
                prev = k
                seen += 1
            leaf = leaf.next
        assert len(chain) == len(leaves_in_order) and all(
            a is b for a, b in zip(chain, leaves_in_order)
        ), "leaf chain does not match the tree's leaf order"
        assert seen == self._size, f"leaf chain holds {seen} keys, len() is {self._size}"

    def __repr__(self) -> str:
        return f"BPlusTree(order={self.order}, size={self._size}, height={self._height})"


class BulkBuilder:
    """정렬된 키로 B+ 트리를 분할 없이 아래에서부터 쌓는다 (벌크 로딩).

    1. :meth:`add` 는 키를 현재 리프에 붙이고, 리프가 채움 목표(order*fill)에 이르면 다음 리프를 시작한다.
    2. :meth:`finish` 는 마지막 리프가 최소 키 수보다 적으면 바로 앞 리프와 재분배(둘이 합쳐 한 노드에 들어가면 병합)하고,
       리프 목록 위에 내부 노드 레벨을 같은 방법으로 쌓아 노드가 하나 남을 때까지 반복한다.
    키 n 개에 O(n) (분할·이동 없음). 키는 엄격히 증가해야 한다 (아니면 ValueError).
    """

    __slots__ = ("order", "leaf_fill_factor", "_target", "_leaves", "_keys", "_values", "_last", "_done")

    def __init__(self, order: int = DEFAULT_ORDER, leaf_fill_factor: float = DEFAULT_LEAF_FILL_FACTOR) -> None:
        _check_params(order, leaf_fill_factor)
        self.order = order
        self.leaf_fill_factor = leaf_fill_factor
        self._target = _leaf_fill_target(order, leaf_fill_factor)
        self._leaves: list[_Leaf] = []
        self._keys = array("q")
        self._values = array("q")
        self._last: int | None = None
        self._done = False

    def add(self, key: int, value: int) -> None:
        """다음 키-값. 키는 직전 키보다 커야 한다."""
        if self._done:
            raise RuntimeError("BulkBuilder.finish() was already called")
        last = self._last
        if last is not None and key <= last:
            raise ValueError(f"bulk load keys must be strictly increasing: {key} after {last}")
        if len(self._keys) == self._target:
            self._seal_leaf()
        self._keys.append(key)
        self._values.append(value)
        self._last = key

    def _seal_leaf(self) -> None:
        leaf = _Leaf(self._keys, self._values)
        if self._leaves:
            self._leaves[-1].next = leaf
        self._leaves.append(leaf)
        self._keys = array("q")
        self._values = array("q")

    def finish(self) -> BPlusTree:
        """트리를 완성해 반환한다. 이후 add/finish 는 쓸 수 없다."""
        if self._done:
            raise RuntimeError("BulkBuilder.finish() was already called")
        self._done = True
        order = self.order
        tree = BPlusTree(order, leaf_fill_factor=self.leaf_fill_factor)
        if self._keys or not self._leaves:
            self._seal_leaf()
        leaves = self._leaves
        size = sum(len(leaf.keys) for leaf in leaves)

        # 마지막 리프 보정: 최소 키 수 미만이면 앞 리프와 재분배 또는 병합
        min_leaf = (order + 1) // 2
        if len(leaves) >= 2 and len(leaves[-1].keys) < min_leaf:
            a, b = leaves[-2], leaves[-1]
            keys = a.keys + b.keys
            values = a.values + b.values
            if len(keys) >= 2 * min_leaf:
                cut = (len(keys) + 1) // 2
                a.keys, a.values = keys[:cut], values[:cut]
                b.keys, b.values = keys[cut:], values[cut:]
            else:  # 둘을 합쳐도 order 이하 (len < 2*min_leaf <= order+1)
                a.keys, a.values = keys, values
                a.next = None
                leaves.pop()

        # 레벨 쌓기: (노드, 서브트리 최소 키)
        level: list[tuple[Any, int]] = [(leaf, leaf.keys[0] if leaf.keys else 0) for leaf in leaves]
        height = 1
        fanout = _internal_fill_target(order, self.leaf_fill_factor) + 1
        min_children = order // 2 + 1
        while len(level) > 1:
            groups = [level[i : i + fanout] for i in range(0, len(level), fanout)]
            if len(groups) >= 2 and len(groups[-1]) < min_children:
                merged = groups[-2] + groups[-1]
                if len(merged) >= 2 * min_children:
                    cut = (len(merged) + 1) // 2
                    groups[-2:] = [merged[:cut], merged[cut:]]
                else:  # 키 수 len(merged)-1 <= 2*(order//2) <= order
                    groups[-2:] = [merged]
            level = [
                (_Internal(array("q", (m for _, m in g[1:])), [n for n, _ in g]), g[0][1]) for g in groups
            ]
            height += 1

        tree._root = level[0][0]
        tree._head = leaves[0]
        tree._size = size
        tree._height = height
        return tree
