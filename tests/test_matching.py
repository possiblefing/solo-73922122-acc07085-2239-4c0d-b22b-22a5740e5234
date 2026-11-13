"""匹配分析引擎的正确性测试：与指数级暴力法交叉验证。"""
from __future__ import annotations

import itertools
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.matching import analyze_graph, hopcroft_karp  # noqa: E402


def hk_size(n_left: int, n_right: int, edges) -> int:
    adj = [[] for _ in range(n_left)]
    for u, v in edges:
        adj[u].append(v)
    return hopcroft_karp(adj, n_left, n_right)[2]


def brute_force(n_left: int, n_right: int, edges):
    """逐边暴力判定：allowed=强制该边后仍可达最大；essential=删该边后最大值下降。"""
    edges = sorted(set(edges))
    max_size = hk_size(n_left, n_right, edges)
    allowed, essential = set(), set()
    for e in edges:
        forced = [x for x in edges if x[0] != e[0] and x[1] != e[1]]
        if hk_size(n_left, n_right, forced) + 1 == max_size:
            allowed.add(e)
        removed = [x for x in edges if x != e]
        if hk_size(n_left, n_right, removed) == max_size - 1:
            essential.add(e)
    nec_left = {u for u in range(n_left) if not any(u == a for a, _ in allowed)}
    nec_right = {v for v in range(n_right) if not any(v == b for _, b in allowed)}
    return max_size, allowed, essential, nec_left, nec_right


def check_graph(n_left: int, n_right: int, edges):
    edges = sorted(set(edges))
    ga = analyze_graph(n_left, n_right, edges)
    max_size, allowed, essential, nec_left, nec_right = brute_force(n_left, n_right, edges)
    assert ga.max_size == max_size, f"max_size {ga.max_size} != {max_size}, edges={edges}"
    assert ga.allowed == allowed, (
        f"allowed mismatch edges={edges}\nextra={ga.allowed - allowed}\nmissing={allowed - ga.allowed}"
    )
    assert ga.essential == essential, (
        f"essential mismatch edges={edges}\nextra={ga.essential - essential}\nmissing={essential - ga.essential}"
    )
    assert set(ga.nec_unmatched_left) == nec_left, f"nec_left mismatch edges={edges}"
    assert set(ga.nec_unmatched_right) == nec_right, f"nec_right mismatch edges={edges}"


def test_hopcroft_karp_vs_permutations():
    """最大匹配大小与排列暴力一致。"""
    rng = random.Random(20261113)
    for _ in range(600):
        n_left = rng.randint(0, 5)
        n_right = rng.randint(0, 5)
        edges = {
            (i, j)
            for i in range(n_left)
            for j in range(n_right)
            if rng.random() < 0.5
        }
        best = 0
        small, big = (n_left, n_right) if n_left <= n_right else (n_right, n_left)
        for perm in itertools.permutations(range(big)):
            cnt = 0
            for i in range(small):
                edge = (i, perm[i]) if n_left <= n_right else (perm[i], i)
                if edge in edges:
                    cnt += 1
            best = max(best, cnt)
        assert hk_size(n_left, n_right, sorted(edges)) == best


def test_exhaustive_3x3():
    """3x3 全部 512 张图穷举。"""
    cells = [(i, j) for i in range(3) for j in range(3)]
    for mask in range(1 << 9):
        edges = [cells[k] for k in range(9) if mask >> k & 1]
        check_graph(3, 3, edges)


def test_random_sparse_and_dense():
    rng = random.Random(1234567)
    for _ in range(2500):
        n_left = rng.randint(1, 8)
        n_right = rng.randint(1, 8)
        p = rng.choice([0.1, 0.25, 0.5, 0.75, 0.95])
        edges = [
            (i, j)
            for i in range(n_left)
            for j in range(n_right)
            if rng.random() < p
        ]
        check_graph(n_left, n_right, edges)


def test_random_interval_graphs():
    """模拟“同额+日期窗”产生的区间二分图。"""
    rng = random.Random(987654)
    for _ in range(1500):
        n_left = rng.randint(1, 10)
        n_right = rng.randint(1, 10)
        amounts = [100, 200, 500]
        left = [(rng.choice(amounts), rng.randint(0, 9)) for _ in range(n_left)]
        right = [(rng.choice(amounts), rng.randint(0, 9)) for _ in range(n_right)]
        edges = [
            (i, j)
            for i in range(n_left)
            for j in range(n_right)
            if left[i][0] == right[j][0] and abs(left[i][1] - right[j][1]) <= 2
        ]
        check_graph(n_left, n_right, edges)


def test_complete_and_pathological():
    for n in (1, 2, 5, 12):
        check_graph(n, n, [(i, j) for i in range(n) for j in range(n)])  # 完全图
        check_graph(n, n, [(i, i) for i in range(n)])  # 唯一完美匹配
        check_graph(n, n, [(i, j) for i in range(n) for j in range(n) if abs(i - j) <= 1])
    # 星形与链形
    check_graph(1, 8, [(0, j) for j in range(8)])
    check_graph(8, 1, [(i, 0) for i in range(8)])
    check_graph(6, 6, [(i, i) for i in range(6)] + [(i, i + 1) for i in range(5)])
