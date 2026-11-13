"""批次分析（含锁定）的语义测试。"""
from __future__ import annotations

import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.analysis import build_edges, compute_analysis  # noqa: E402


def mkrows(prefix, specs):
    return [
        {"row_id": f"{prefix}{suffix}", "rdate": d, "currency": c, "amount_cents": a}
        for suffix, d, c, a in specs
    ]


def test_amount_ambiguity():
    """同额歧义：2x2 完全候选下没有任何必然配对，但全部配对都可能。"""
    bank = mkrows("B", [("001", "2026-03-10", "CNY", 50000),
                        ("002", "2026-03-11", "CNY", 50000),
                        ("003", "2026-03-12", "CNY", 8800)])
    system = mkrows("S", [("001", "2026-03-10", "CNY", 50000),
                          ("002", "2026-03-12", "CNY", 50000),
                          ("003", "2026-03-13", "CNY", 8800)])
    analysis, feasible, max_size = compute_analysis(bank, system, [])
    assert feasible and max_size == 3
    assert analysis["confirmed_pairs"] == [
        {"left_id": "B003", "right_id": "S003", "source": "all_optimal_solutions"}
    ]
    assert analysis["possible_pairs_by_bank_row"] == {
        "B001": ["S001", "S002"],
        "B002": ["S001", "S002"],
    }
    assert analysis["possible_pairs_by_system_row"] == {
        "S001": ["B001", "B002"],
        "S002": ["B001", "B002"],
    }
    assert analysis["necessarily_unmatched_bank_rows"] == []
    assert analysis["necessarily_unmatched_system_rows"] == []


def test_path_graph_essential_and_infeasible_lock():
    """链式候选：两端为必然配对，中间边存在但不属于任何最优方案。"""
    bank = mkrows("B", [("101", "2026-04-01", "CNY", 1000),
                        ("102", "2026-04-03", "CNY", 1000)])
    system = mkrows("S", [("101", "2026-04-01", "CNY", 1000),
                          ("102", "2026-04-05", "CNY", 1000)])
    analysis, feasible, max_size = compute_analysis(bank, system, [])
    assert feasible and max_size == 2
    # B101-S101 与 B102-S102 为所有最优方案共有
    assert {(p["left_id"], p["right_id"]) for p in analysis["confirmed_pairs"]} == {
        ("B101", "S101"), ("B102", "S102"),
    }
    # B102-S101 是候选边但不可参与任何最优方案
    assert "B102" not in analysis["possible_pairs_by_bank_row"]
    # 锁定该边不可行
    _, feasible, _ = compute_analysis(bank, system, [("B102", "S101")])
    assert not feasible
    # 锁定必然配对可行
    analysis2, feasible2, _ = compute_analysis(bank, system, [("B101", "S101")])
    assert feasible2
    assert analysis2["confirmed_pairs"][0] == {
        "left_id": "B101", "right_id": "S101", "source": "locked",
    }


def test_date_window_and_currency_amount_rules():
    bank = mkrows("B", [("1", "2026-01-10", "CNY", 100),
                        ("2", "2026-01-15", "CNY", 100),   # 差 3 天，不可配 S1
                        ("3", "2026-01-10", "USD", 100),   # 币种不同
                        ("4", "2026-01-10", "CNY", 200)])  # 金额不同
    system = mkrows("S", [("1", "2026-01-12", "CNY", 100)])  # 与 B1 差 2 天
    edges = build_edges(bank, system)
    assert edges == [(0, 0)]
    analysis, feasible, max_size = compute_analysis(bank, system, [])
    assert feasible and max_size == 1
    assert analysis["necessarily_unmatched_bank_rows"] == ["B2", "B3", "B4"]


def test_necessarily_unmatched_rows():
    bank = mkrows("B", [("1", "2026-01-10", "CNY", 100),
                        ("2", "2026-01-10", "CNY", 100),
                        ("3", "2026-01-10", "CNY", 999)])
    system = mkrows("S", [("1", "2026-01-10", "CNY", 100)])
    analysis, feasible, max_size = compute_analysis(bank, system, [])
    assert feasible and max_size == 1
    # B1/B2 都可能被匹配，B3 金额不同必然未匹配
    assert analysis["necessarily_unmatched_bank_rows"] == ["B3"]
    assert analysis["confirmed_pairs"] == []
    assert analysis["possible_pairs_by_bank_row"] == {"B1": ["S1"], "B2": ["S1"]}


def test_lock_feasibility_equals_allowed_random():
    """随机图上：某配对可锁定 ⟺ 它出现在某个最优方案中。"""
    rng = random.Random(2026)
    for _ in range(300):
        n_b = rng.randint(1, 6)
        n_s = rng.randint(1, 6)
        bank = mkrows("B", [(str(i), f"2026-01-{rng.randint(1, 9):02d}",
                             rng.choice(["CNY", "USD"]), rng.choice([100, 200]))
                            for i in range(n_b)])
        system = mkrows("S", [(str(j), f"2026-01-{rng.randint(1, 9):02d}",
                               rng.choice(["CNY", "USD"]), rng.choice([100, 200]))
                              for j in range(n_s)])
        analysis, feasible, max_size = compute_analysis(bank, system, [])
        assert feasible
        allowed = {
            (l, r)
            for l, rs in analysis["possible_pairs_by_bank_row"].items()
            for r in rs
        } | {
            (p["left_id"], p["right_id"]) for p in analysis["confirmed_pairs"]
        }
        for l, r in allowed:
            _, ok, _ = compute_analysis(bank, system, [(l, r)])
            assert ok, f"allowed pair {(l, r)} should be lockable"
        # 非候选边直接抛错路径由 API 层校验，这里验证候选但不可行的边
        edges = build_edges(bank, system)
        idx_l = {row["row_id"]: i for i, row in enumerate(bank)}
        idx_r = {row["row_id"]: i for i, row in enumerate(system)}
        for (i, j) in edges:
            pair = (bank[i]["row_id"], system[j]["row_id"])
            if pair not in allowed:
                _, ok, _ = compute_analysis(bank, system, [pair])
                assert not ok, f"non-allowed edge {pair} must be infeasible"


def test_multiple_locks_and_unlock_consistency():
    bank = mkrows("B", [(str(i), "2026-02-0%d" % (i + 1), "CNY", 700) for i in range(3)])
    system = mkrows("S", [(str(i), "2026-02-0%d" % (i + 1), "CNY", 700) for i in range(3)])
    # 完全图 K3,3：任意锁定两个不同行配对均可行
    analysis, feasible, max_size = compute_analysis(
        bank, system, [("B0", "S2"), ("B2", "S0")]
    )
    assert feasible and max_size == 3
    locked = [p for p in analysis["confirmed_pairs"] if p["source"] == "locked"]
    assert len(locked) == 2
    # 剩余一行 B1 只能配 S1，成为所有最优方案共有
    essential = [p for p in analysis["confirmed_pairs"] if p["source"] == "all_optimal_solutions"]
    assert essential == [{"left_id": "B1", "right_id": "S1", "source": "all_optimal_solutions"}]
