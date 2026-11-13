"""API 端到端测试：上传校验、锁定/撤销、乐观修订并发、共库与持久化。"""
from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

AMBIGUITY_BANK = b"""id,date,currency,amount_cents
B001,2026-03-10,CNY,50000
B002,2026-03-11,CNY,50000
B003,2026-03-12,CNY,8800
"""
AMBIGUITY_SYSTEM = b"""id,date,currency,amount_cents
S001,2026-03-10,CNY,50000
S002,2026-03-12,CNY,50000
S003,2026-03-13,CNY,8800
"""
PATH_BANK = b"""id,date,currency,amount_cents
B101,2026-04-01,CNY,1000
B102,2026-04-03,CNY,1000
"""
PATH_SYSTEM = b"""id,date,currency,amount_cents
S101,2026-04-01,CNY,1000
S102,2026-04-05,CNY,1000
"""


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RECON_DB", str(tmp_path / "recon.db"))
    init_db()
    with TestClient(app) as c:
        c.recon_db = str(tmp_path / "recon.db")
        yield c


def create(client, bank=AMBIGUITY_BANK, system=AMBIGUITY_SYSTEM):
    resp = client.post(
        "/batches",
        files={
            "bank_file": ("bank.csv", bank, "text/csv"),
            "system_file": ("system.csv", system, "text/csv"),
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_and_analyze_ambiguity(client):
    state = create(client)
    assert state["revision"] == 1
    analysis = state["analysis"]
    assert analysis["max_matching_size"] == 3
    # 同额歧义组没有必然配对；独立组 B003-S003 为所有最优方案共有
    assert analysis["confirmed_pairs"] == [
        {"left_id": "B003", "right_id": "S003", "source": "all_optimal_solutions"}
    ]
    assert analysis["possible_pairs_by_bank_row"] == {
        "B001": ["S001", "S002"],
        "B002": ["S001", "S002"],
    }
    assert analysis["necessarily_unmatched_bank_rows"] == []
    # 查询接口返回一致状态
    got = client.get(f"/batches/{state['batch_id']}")
    assert got.status_code == 200 and got.json() == state


def test_csv_validation(client):
    bad_cases = [
        (b"id,date,currency,amount_cents\nB1,2026-01-01,CNY,-5\n", AMBIGUITY_SYSTEM),  # 非正金额
        (b"id,date,currency,amount_cents\nB1,2026-13-01,CNY,5\n", AMBIGUITY_SYSTEM),   # 非法日期
        (b"id,date,currency,amount_cents\nB1,2026-01-01,CNY,5\nB1,2026-01-02,CNY,6\n", AMBIGUITY_SYSTEM),  # 重复ID
        (b"id,date,currency\nB1,2026-01-01,CNY\n", AMBIGUITY_SYSTEM),                  # 缺列
        (b"id,date,currency,amount_cents\n" + b"".join(
            f"B{i},2026-01-01,CNY,1\n".encode() for i in range(101)), AMBIGUITY_SYSTEM),  # 超 100 行
        (b"id,date,currency,amount_cents\nB1,2026-01-01,CNY,10.5\n", AMBIGUITY_SYSTEM),  # 非整数
    ]
    for bank, system in bad_cases:
        resp = client.post(
            "/batches",
            files={"bank_file": ("b.csv", bank, "text/csv"),
                   "system_file": ("s.csv", system, "text/csv")},
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["error"] == "invalid_csv"


def test_lock_flow_and_stale_revision(client):
    state = create(client)
    bid = state["batch_id"]
    rev = state["revision"]

    # 锁定一个可参与最优方案的配对
    resp = client.post(f"/batches/{bid}/locks",
                       json={"left_id": "B001", "right_id": "S001", "expected_revision": rev})
    assert resp.status_code == 200, resp.text
    state2 = resp.json()
    assert state2["revision"] == rev + 1
    assert state2["locks"] == [{"left_id": "B001", "right_id": "S001"}]
    confirmed = {(p["left_id"], p["right_id"]) for p in state2["analysis"]["confirmed_pairs"]}
    assert ("B001", "S001") in confirmed
    # 锁定后 B002 只能配 S002（成为所有最优方案共有）
    assert ("B002", "S002") in confirmed

    # 持旧修订号的修改被拒绝
    resp = client.post(f"/batches/{bid}/locks",
                       json={"left_id": "B002", "right_id": "S002", "expected_revision": rev})
    assert resp.status_code == 409
    assert resp.json()["detail"]["current_revision"] == rev + 1

    # 正确修订号可继续锁定
    resp = client.post(f"/batches/{bid}/locks",
                       json={"left_id": "B002", "right_id": "S002", "expected_revision": rev + 1})
    assert resp.status_code == 200

    # 撤销锁定（先取最新修订）
    cur = client.get(f"/batches/{bid}").json()["revision"]
    resp = client.post(f"/batches/{bid}/unlocks",
                       json={"left_id": "B001", "expected_revision": cur})
    assert resp.status_code == 200
    assert resp.json()["locks"] == [{"left_id": "B002", "right_id": "S002"}]

    # 撤销不存在的锁定
    cur = client.get(f"/batches/{bid}").json()["revision"]
    resp = client.post(f"/batches/{bid}/unlocks",
                       json={"left_id": "B001", "expected_revision": cur})
    assert resp.status_code == 404


def test_lock_rejections_keep_state(client):
    state = create(client, PATH_BANK, PATH_SYSTEM)
    bid = state["batch_id"]
    rev = state["revision"]

    # B102-S101 是候选边，但锁定后无法达到原最大配对数 2
    resp = client.post(f"/batches/{bid}/locks",
                       json={"left_id": "B102", "right_id": "S101", "expected_revision": rev})
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "lock_infeasible"

    # 非候选配对（日期差 4 天）
    resp = client.post(f"/batches/{bid}/locks",
                       json={"left_id": "B101", "right_id": "S102", "expected_revision": rev})
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "not_a_candidate_pair"

    # 不存在的行
    resp = client.post(f"/batches/{bid}/locks",
                       json={"left_id": "B999", "right_id": "S101", "expected_revision": rev})
    assert resp.status_code == 422

    # 状态保持原状
    cur = client.get(f"/batches/{bid}").json()
    assert cur["revision"] == rev and cur["locks"] == []

    # 锁定必然配对可行；之后重复锁定同一行被拒
    resp = client.post(f"/batches/{bid}/locks",
                       json={"left_id": "B101", "right_id": "S101", "expected_revision": rev})
    assert resp.status_code == 200
    resp = client.post(f"/batches/{bid}/locks",
                       json={"left_id": "B101", "right_id": "S101", "expected_revision": rev + 1})
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "already_locked"


def test_concurrent_locks_single_winner(client):
    """多客户端持同一修订号并发锁定：恰好一个成功，其余 409。"""
    state = create(client)
    bid = state["batch_id"]
    rev = state["revision"]
    pairs = [("B001", "S001"), ("B002", "S002"), ("B001", "S002"), ("B002", "S001")]
    barrier = threading.Barrier(len(pairs))
    results = []

    def worker(pair):
        c = TestClient(app)
        barrier.wait()
        r = c.post(f"/batches/{bid}/locks",
                   json={"left_id": pair[0], "right_id": pair[1], "expected_revision": rev})
        results.append(r.status_code)

    threads = [threading.Thread(target=worker, args=(p,)) for p in pairs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(200) == 1
    assert results.count(409) == len(pairs) - 1
    # 最终状态一致：仅一个锁，修订号 +1
    final = client.get(f"/batches/{bid}").json()
    assert final["revision"] == rev + 1
    assert len(final["locks"]) == 1


def test_shared_db_between_two_app_instances(client):
    """两个应用实例（同库文件）互见状态，模拟两个 API 进程共库。"""
    state = create(client)
    bid = state["batch_id"]
    with TestClient(app) as other:
        got = other.get(f"/batches/{bid}")
        assert got.status_code == 200
        assert got.json()["revision"] == state["revision"]
        # 另一实例锁定后，本实例立即可见
        resp = other.post(f"/batches/{bid}/locks",
                          json={"left_id": "B001", "right_id": "S001",
                                "expected_revision": state["revision"]})
        assert resp.status_code == 200
    cur = client.get(f"/batches/{bid}").json()
    assert cur["revision"] == state["revision"] + 1
    assert cur["locks"] == [{"left_id": "B001", "right_id": "S001"}]


def test_persistence_across_restart(client):
    """重新初始化（模拟进程重启）后状态保留。"""
    state = create(client)
    bid = state["batch_id"]
    client.post(f"/batches/{bid}/locks",
                json={"left_id": "B001", "right_id": "S001", "expected_revision": 1})
    before = client.get(f"/batches/{bid}").json()
    # 模拟重启：重新走启动流程（重连同一数据库文件）
    init_db()
    with TestClient(app) as restarted:
        after = restarted.get(f"/batches/{bid}").json()
    assert after == before
    assert after["locks"] == [{"left_id": "B001", "right_id": "S001"}]


def test_list_batches(client):
    create(client)
    create(client, PATH_BANK, PATH_SYSTEM)
    resp = client.get("/batches")
    assert resp.status_code == 200
    listing = resp.json()
    assert len(listing) == 2
    assert {b["max_matching_size"] for b in listing} == {3, 2}


def test_unknown_batch(client):
    assert client.get("/batches/nope").status_code == 404
    resp = client.post("/batches/nope/locks",
                       json={"left_id": "a", "right_id": "b", "expected_revision": 1})
    assert resp.status_code == 404
