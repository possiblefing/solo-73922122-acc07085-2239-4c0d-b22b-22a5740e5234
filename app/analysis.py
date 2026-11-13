"""对账批次分析：在人工锁定约束下计算最大配对与最优方案集合。

行记录为 dict：{"row_id", "rdate"(YYYY-MM-DD), "currency", "amount_cents"}。
配对规则：币种相同、金额相同、入账日期相差不超过 2 天。
锁定语义：锁定的配对必须出现在最终方案中；若存在锁定时无法达到
原始最大配对数，则该锁定不可行（分析结果 feasible=False）。
"""
from __future__ import annotations

from datetime import date

from .matching import analyze_graph, hopcroft_karp

DATE_WINDOW_DAYS = 2


def build_edges(bank_rows: list[dict], system_rows: list[dict]) -> list[tuple[int, int]]:
    """生成全部候选配对边（返回两侧行下标对）。"""
    bank_key = [
        (r["currency"], r["amount_cents"], date.fromisoformat(r["rdate"]).toordinal())
        for r in bank_rows
    ]
    sys_key = [
        (r["currency"], r["amount_cents"], date.fromisoformat(r["rdate"]).toordinal())
        for r in system_rows
    ]
    edges = []
    for i, (cur_b, amt_b, day_b) in enumerate(bank_key):
        for j, (cur_s, amt_s, day_s) in enumerate(sys_key):
            if cur_b == cur_s and amt_b == amt_s and abs(day_b - day_s) <= DATE_WINDOW_DAYS:
                edges.append((i, j))
    return edges


def max_matching_size(bank_rows: list[dict], system_rows: list[dict]) -> int:
    edges = build_edges(bank_rows, system_rows)
    adj = [[] for _ in range(len(bank_rows))]
    for u, v in edges:
        adj[u].append(v)
    return hopcroft_karp(adj, len(bank_rows), len(system_rows))[2]


def compute_analysis(
    bank_rows: list[dict],
    system_rows: list[dict],
    locks: list[tuple[str, str]],
) -> tuple[dict, bool, int]:
    """计算批次分析结果。

    返回 (analysis, feasible, max_size)：
      * analysis  —— 可直接 JSON 序列化的分析结果；
      * feasible  —— 全部锁定同时满足时仍能达到原始最大配对数；
      * max_size  —— 原始（无锁定）最大配对数。
    """
    left_ids = [r["row_id"] for r in bank_rows]
    right_ids = [r["row_id"] for r in system_rows]
    idx_left = {rid: i for i, rid in enumerate(left_ids)}
    idx_right = {rid: i for i, rid in enumerate(right_ids)}

    edges = build_edges(bank_rows, system_rows)
    adj = [[] for _ in range(len(bank_rows))]
    for u, v in edges:
        adj[u].append(v)
    max_size = hopcroft_karp(adj, len(bank_rows), len(system_rows))[2]

    lock_pairs: list[tuple[int, int]] = []
    seen_left: set[int] = set()
    seen_right: set[int] = set()
    for left_id, right_id in locks:
        u = idx_left[left_id]
        v = idx_right[right_id]
        if u in seen_left or v in seen_right:
            raise ValueError("locked pairs share a row")
        seen_left.add(u)
        seen_right.add(v)
        lock_pairs.append((u, v))

    # 扣除锁定行后的剩余图 G'
    free_left = [i for i in range(len(bank_rows)) if i not in seen_left]
    free_right = [j for j in range(len(system_rows)) if j not in seen_right]
    map_left = {old: new for new, old in enumerate(free_left)}
    map_right = {old: new for new, old in enumerate(free_right)}
    reduced_edges = [
        (map_left[u], map_right[v])
        for (u, v) in edges
        if u in map_left and v in map_right
    ]

    ga = analyze_graph(len(free_left), len(free_right), reduced_edges)
    feasible = ga.max_size + len(lock_pairs) == max_size

    confirmed = [
        {"left_id": left_id, "right_id": right_id, "source": "locked"}
        for left_id, right_id in locks
    ]
    for u, v in sorted(ga.essential):
        confirmed.append(
            {
                "left_id": left_ids[free_left[u]],
                "right_id": right_ids[free_right[v]],
                "source": "all_optimal_solutions",
            }
        )

    # “其余行”的候选配对：已确定（锁定/所有最优方案共有）的行不再重复列出。
    # 注：有必然配对的行其唯一可选伙伴就是该必然配对，故按任一端点过滤即可。
    confirmed_left = {p["left_id"] for p in confirmed}
    confirmed_right = {p["right_id"] for p in confirmed}
    possible_by_left: dict[str, list[str]] = {}
    possible_by_right: dict[str, list[str]] = {}
    for u, v in sorted(ga.allowed):
        lid = left_ids[free_left[u]]
        rid = right_ids[free_right[v]]
        if lid in confirmed_left or rid in confirmed_right:
            continue
        possible_by_left.setdefault(lid, []).append(rid)
        possible_by_right.setdefault(rid, []).append(lid)

    analysis = {
        "max_matching_size": max_size,
        "locked_pair_count": len(lock_pairs),
        "confirmed_pairs": confirmed,
        "possible_pairs_by_bank_row": possible_by_left,
        "possible_pairs_by_system_row": possible_by_right,
        "necessarily_unmatched_bank_rows": [
            left_ids[free_left[u]] for u in ga.nec_unmatched_left
        ],
        "necessarily_unmatched_system_rows": [
            right_ids[free_right[v]] for v in ga.nec_unmatched_right
        ],
    }
    return analysis, feasible, max_size
