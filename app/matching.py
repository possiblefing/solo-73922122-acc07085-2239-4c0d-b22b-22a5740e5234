"""二分图最大匹配与最优方案集合分析。

理论依据（Dulmage–Mendelsohn 分解的交错路刻画）：

设 M 为图 G 的任一最大匹配。构造有向图 D：
  * 未在 M 中的边 (u,v) 定向为 L→R（u→v）；
  * 已在 M 中的边 (u,v) 定向为 R→L（v→u）。

另一最大匹配 M' 与 M 的对称差 M △ M' 只由两类结构组成：
  1. 偶长交错环            —— 对应 D 中的有向环（同一强连通分量）；
  2. 等长交错偶路径        —— 一端为 M 中自由点、另一端为 M' 中自由点，
                              且两个端点必在同侧。

由此可得精确判定（均为充要条件）：
  * 未匹配边 (u,v) 出现在某个最大匹配中 ⟺
        u,v 同属一个强连通分量
        或 u 可由自由 L 点经有向路径到达（u ∈ Z_out）
        或 u 可由某个已匹配 R 点到达（u ∈ Z_Rout）且 v 可达自由 R 点（v ∈ Z_in）。
  * 已匹配边 (u,v) 出现在所有最大匹配中（必然配对）⟺ 不满足以下任一条：
        u,v 同属一个强连通分量
        或 v ∈ Z_out
        或 v ∈ Z_Rout 且 u ∈ Z_in。
  * 顶点可能未被匹配 ⟺ 它没有关联的“可出现在某最大匹配中”的边。

本模块只依赖标准库，行数上限 100 时计算量为毫秒级。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass


def hopcroft_karp(adj: list[list[int]], n_left: int, n_right: int):
    """Hopcroft–Karp 最大匹配。adj[u] 为左点 u 的右点邻表。

    返回 (match_left, match_right, matching_size)。
    """
    match_left = [-1] * n_left
    match_right = [-1] * n_right
    INF = 10 ** 9

    def bfs():
        dist = [INF] * n_left
        queue = deque()
        for u in range(n_left):
            if match_left[u] == -1:
                dist[u] = 0
                queue.append(u)
        found = False
        while queue:
            u = queue.popleft()
            for v in adj[u]:
                w = match_right[v]
                if w == -1:
                    found = True
                elif dist[w] == INF:
                    dist[w] = dist[u] + 1
                    queue.append(w)
        return found, dist

    def dfs(u: int, dist: list[int]) -> bool:
        for v in adj[u]:
            w = match_right[v]
            if w == -1 or (dist[w] == dist[u] + 1 and dfs(w, dist)):
                match_left[u] = v
                match_right[v] = u
                return True
        dist[u] = INF
        return False

    size = 0
    while True:
        found, dist = bfs()
        if not found:
            break
        for u in range(n_left):
            if match_left[u] == -1 and dfs(u, dist):
                size += 1
    return match_left, match_right, size


def _tarjan_scc(adj: list[list[int]]) -> list[int]:
    """迭代版 Tarjan 强连通分量，返回每个顶点所属分量编号。"""
    n = len(adj)
    index = [0] * n
    low = [0] * n
    on_stack = [False] * n
    comp = [-1] * n
    stack: list[int] = []
    counter = 0
    ncomp = 0
    for s in range(n):
        if index[s]:
            continue
        work = [(s, 0)]
        while work:
            v, i = work[-1]
            if i == 0:
                counter += 1
                index[v] = low[v] = counter
                stack.append(v)
                on_stack[v] = True
            if i < len(adj[v]):
                w = adj[v][i]
                work[-1] = (v, i + 1)
                if index[w] == 0:
                    work.append((w, 0))
                elif on_stack[w] and index[w] < low[v]:
                    low[v] = index[w]
            else:
                if low[v] == index[v]:
                    while True:
                        w = stack.pop()
                        on_stack[w] = False
                        comp[w] = ncomp
                        if w == v:
                            break
                    ncomp += 1
                work.pop()
                if work:
                    p = work[-1][0]
                    if low[v] < low[p]:
                        low[p] = low[v]
    return comp


def _reachable(adj: list[list[int]], sources) -> bytearray:
    seen = bytearray(len(adj))
    queue = deque()
    for s in sources:
        if not seen[s]:
            seen[s] = 1
            queue.append(s)
    while queue:
        v = queue.popleft()
        for w in adj[v]:
            if not seen[w]:
                seen[w] = 1
                queue.append(w)
    return seen


@dataclass
class GraphAnalysis:
    """一张二分图的最优方案集合分析结果（顶点用 0 基下标）。"""

    max_size: int
    allowed: set[tuple[int, int]]      # 出现在某个最大匹配中的边
    essential: set[tuple[int, int]]    # 出现在所有最大匹配中的边
    nec_unmatched_left: list[int]      # 任何最大匹配都匹配不到的左点
    nec_unmatched_right: list[int]     # 任何最大匹配都匹配不到的右点


def analyze_graph(n_left: int, n_right: int, edges) -> GraphAnalysis:
    """对二分图做全量最优方案分析，时间复杂度 O(V·E) 级别内的一次性 SCC/可达性计算。"""
    edge_set = sorted(set(edges))
    for u, v in edge_set:
        if not (0 <= u < n_left and 0 <= v < n_right):
            raise ValueError(f"edge out of range: {(u, v)}")

    adj = [[] for _ in range(n_left)]
    for u, v in edge_set:
        adj[u].append(v)
    match_left, match_right, max_size = hopcroft_karp(adj, n_left, n_right)

    matched_pairs = {(u, match_left[u]) for u in range(n_left) if match_left[u] != -1}

    # 有向图 D：顶点 0..n_left-1 为 L，n_left..n_left+n_right-1 为 R
    n = n_left + n_right
    d_adj = [[] for _ in range(n)]
    for u, v in edge_set:
        if (u, v) in matched_pairs:
            d_adj[n_left + v].append(u)   # 匹配边：R→L
        else:
            d_adj[u].append(n_left + v)   # 未匹配边：L→R

    comp = _tarjan_scc(d_adj)

    free_left = [u for u in range(n_left) if match_left[u] == -1]
    free_right = [n_left + v for v in range(n_right) if match_right[v] == -1]
    matched_right = [n_left + v for v in range(n_right) if match_right[v] != -1]

    # Z_out：自由 L 点沿 D 可达；Z_in：可沿 D 到达自由 R 点；Z_Rout：已匹配 R 点沿 D 可达
    z_out = _reachable(d_adj, free_left)
    rev_adj = [[] for _ in range(n)]
    for v in range(n):
        for w in d_adj[v]:
            rev_adj[w].append(v)
    z_in = _reachable(rev_adj, free_right)
    z_rout = _reachable(d_adj, matched_right)

    allowed: set[tuple[int, int]] = set()
    essential: set[tuple[int, int]] = set()
    for u, v in edge_set:
        rv = n_left + v
        if (u, v) in matched_pairs:
            allowed.add((u, v))
            if not (
                comp[u] == comp[rv]
                or z_out[rv]
                or (z_rout[rv] and z_in[u])
            ):
                essential.add((u, v))
        else:
            if comp[u] == comp[rv] or z_out[u] or (z_rout[u] and z_in[rv]):
                allowed.add((u, v))

    has_allowed_left = [False] * n_left
    has_allowed_right = [False] * n_right
    for u, v in allowed:
        has_allowed_left[u] = True
        has_allowed_right[v] = True

    return GraphAnalysis(
        max_size=max_size,
        allowed=allowed,
        essential=essential,
        nec_unmatched_left=[u for u in range(n_left) if not has_allowed_left[u]],
        nec_unmatched_right=[v for v in range(n_right) if not has_allowed_right[v]],
    )
