#!/usr/bin/env python3
"""对账 API 演示：同额歧义 + 并发锁定 + 不可行锁定拒绝。

用法：
    python3 samples/demo.py [base_url]
    默认 base_url 为 http://localhost:8000（docker compose 的网关入口，
    请求会在 api1/api2 两个进程间轮询，演示两进程共库）。

仅依赖 Python 标准库。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request
import uuid

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
HERE = os.path.dirname(os.path.abspath(__file__))


def request(method: str, path: str, body: dict | None = None, files: dict | None = None):
    url = BASE + path
    headers = {}
    data = None
    if files is not None:
        boundary = uuid.uuid4().hex
        parts = []
        for name, (filename, content) in files.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\nContent-Type: text/csv\r\n\r\n'.encode()
            )
            parts.append(content if isinstance(content, bytes) else content.encode())
            parts.append(b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        data = b"".join(parts)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def show(title: str):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def load_sample(name: str):
    with open(os.path.join(HERE, name, "bank.csv"), "rb") as f:
        bank = f.read()
    with open(os.path.join(HERE, name, "system.csv"), "rb") as f:
        system = f.read()
    return {"bank_file": ("bank.csv", bank), "system_file": ("system.csv", system)}


def brief(obj, limit=240):
    text = json.dumps(obj, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "..."


def main():
    show("样例 1：同额歧义 —— 两笔 50000 分同币种款项，日期两两相差≤2天")
    status, state = request("POST", "/batches", files=load_sample("01_amount_ambiguity"))
    assert status == 201, state
    batch_id = state["batch_id"]
    revision = state["revision"]
    analysis = state["analysis"]
    print(f"批次 {batch_id}  修订号 {revision}")
    print(f"整批最大配对数: {analysis['max_matching_size']}")
    print(f"确定配对（所有最优方案共有）: {brief(analysis['confirmed_pairs'])}")
    print("可能配对（其余行可参与最优方案的配对）:")
    for row, partners in analysis["possible_pairs_by_bank_row"].items():
        print(f"  {row} -> {partners}")
    print(f"必然未匹配行: 银行侧 {analysis['necessarily_unmatched_bank_rows']}  "
          f"系统侧 {analysis['necessarily_unmatched_system_rows']}")
    print(">> 同额的 B001/B002 与 S001/S002 构成歧义：没有任何一对被任选为确定，")
    print("   只有所有最优方案共有的 B003-S003 被标为确定。")

    show("样例 2：并发锁定 —— 两个客户端持同一修订号同时锁定，恰好一个成功")
    results = []
    barrier = threading.Barrier(2)

    def worker(pair, tag):
        barrier.wait()
        st, resp = request(
            "POST", f"/batches/{batch_id}/locks",
            body={"left_id": pair[0], "right_id": pair[1], "expected_revision": revision},
        )
        results.append((tag, st, resp))

    t1 = threading.Thread(target=worker, args=(("B001", "S001"), "客户端A 锁 B001-S001"))
    t2 = threading.Thread(target=worker, args=(("B002", "S002"), "客户端B 锁 B002-S002"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    for tag, st, resp in sorted(results):
        print(f"  {tag}: HTTP {st}  {brief(resp, 160)}")
    assert sorted(st for _, st, _ in results) == [200, 409], results
    print(">> 后到的请求因修订号过期被拒（409），状态未被破坏。")

    show("样例 2 续：读取最新修订号后重试，锁定成功")
    status, state = request("GET", f"/batches/{batch_id}")
    assert status == 200
    fresh = state["revision"]
    loser = [r for r in results if r[1] == 409][0]
    pair = ("B002", "S002") if "B002" in loser[0] else ("B001", "S001")
    status, state = request(
        "POST", f"/batches/{batch_id}/locks",
        body={"left_id": pair[0], "right_id": pair[1], "expected_revision": fresh},
    )
    print(f"  携带最新修订号 {fresh} 重试 {pair}: HTTP {status}")
    assert status == 200, state
    print(f"  当前锁定: {brief(state['locks'])}  修订号: {state['revision']}")

    show("样例 2 续：撤销锁定（同样携带修订号）")
    status, state = request(
        "POST", f"/batches/{batch_id}/unlocks",
        body={"left_id": pair[0], "expected_revision": state["revision"]},
    )
    print(f"  撤销 {pair[0]} 的锁定: HTTP {status}  当前锁定: {state['locks']}")
    assert status == 200, state

    show("样例 3：不可行锁定 —— 候选边但会破坏最大配对数，拒绝并保留原状")
    status, state2 = request("POST", "/batches", files=load_sample("02_concurrent_lock"))
    assert status == 201, state2
    bid2 = state2["batch_id"]
    print(f"批次 {bid2}  最大配对数: {state2['analysis']['max_matching_size']}")
    print(f"  确定配对: {brief(state2['analysis']['confirmed_pairs'])}")
    status, resp = request(
        "POST", f"/batches/{bid2}/locks",
        body={"left_id": "B102", "right_id": "S101", "expected_revision": state2["revision"]},
    )
    print(f"  尝试锁定 B102-S101（候选边，但锁后最大配对数将降为 1）: HTTP {status}")
    print(f"  响应: {brief(resp)}")
    assert status == 422, resp
    status, after = request("GET", f"/batches/{bid2}")
    assert after["locks"] == [] and after["revision"] == state2["revision"]
    print(">> 锁定被拒绝，批次状态保持原状。")

    show("演示完成")
    print(f"批次 1: {batch_id}（同额歧义 + 并发锁定）")
    print(f"批次 2: {bid2}（不可行锁定拒绝）")
    print(f"可打开 {BASE}/docs 交互式查看与操作。")


if __name__ == "__main__":
    main()
