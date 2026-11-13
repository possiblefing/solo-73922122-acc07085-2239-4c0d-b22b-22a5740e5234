"""银行到账流水 ↔ 收款系统明细 对账 API。

配对规则：币种相同、金额（正整数分）相同、入账日期相差不超过 2 天，每行最多配一次。
分析口径：整批最大配对数；只把所有最优方案共有的配对（及人工锁定）标为确定；
其余行列出可参与最优方案的候选配对；任何最优方案都匹配不到的行单列；不任选方案。
并发口径：修改须携带所见修订号，过期拒绝；锁定/修订/分析结果同事务生效；
计算不持写锁；查询走一致性快照；状态落盘，重启保留。
"""
from __future__ import annotations

import json
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import RedirectResponse

from .analysis import build_edges, compute_analysis
from .csvio import CsvError, parse_upload
from .db import connect, init_db, load_batch_inputs, snapshot, write_txn
from .schemas import (
    BatchState,
    BatchSummary,
    LockRequest,
    UnlockRequest,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="银行流水对账 API",
    description=(
        "上传银行到账流水与收款系统明细两份 CSV（各≤100 行），"
        "按“币种+金额相同、日期差≤2 天”求整批最大配对；"
        "仅将所有最优方案共有的配对标为确定，支持人工锁定/撤销（乐观修订并发控制）。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


class _StaleRevision(Exception):
    """写事务内发现修订号已过期。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _build_state(
    batch_id: str,
    revision: int,
    created_at: str,
    bank_rows: list[dict],
    system_rows: list[dict],
    locks: list[tuple[str, str]],
    analysis: dict,
) -> dict:
    return {
        "batch_id": batch_id,
        "revision": revision,
        "created_at": created_at,
        "bank_rows": [
            {
                "id": r["row_id"],
                "date": r["rdate"],
                "currency": r["currency"],
                "amount_cents": r["amount_cents"],
            }
            for r in bank_rows
        ],
        "system_rows": [
            {
                "id": r["row_id"],
                "date": r["rdate"],
                "currency": r["currency"],
                "amount_cents": r["amount_cents"],
            }
            for r in system_rows
        ],
        "locks": [{"left_id": l, "right_id": r} for l, r in locks],
        "analysis": analysis,
    }


def _conflict(current_revision: int | None) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "error": "revision_conflict",
            "message": "修订号已过期：状态已被其他请求修改，请重新获取后再试",
            "current_revision": current_revision,
        },
    )


def _current_revision(conn, batch_id: str) -> int | None:
    row = conn.execute("SELECT revision FROM batches WHERE id = ?", (batch_id,)).fetchone()
    return row["revision"] if row else None


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.get("/health", include_in_schema=False)
def health():
    return {"status": "ok"}


@app.post(
    "/batches",
    status_code=201,
    response_model=BatchState,
    summary="上传两份 CSV 创建对账批次",
)
async def create_batch(bank_file: UploadFile = File(...), system_file: UploadFile = File(...)):
    """bank_file 为银行到账流水，system_file 为收款系统明细；

    均须含表头 id,date,currency,amount_cents，各不超过 100 行。"""
    try:
        bank_rows = parse_upload(await bank_file.read(), "bank_file")
        system_rows = parse_upload(await system_file.read(), "system_file")
    except CsvError as exc:
        raise HTTPException(
            status_code=422, detail={"error": "invalid_csv", "messages": exc.messages}
        )

    batch_id = secrets.token_hex(8)
    created_at = _now()
    analysis, feasible, max_size = compute_analysis(bank_rows, system_rows, [])
    assert feasible  # 无锁定时必然可行
    state = _build_state(batch_id, 1, created_at, bank_rows, system_rows, [], analysis)

    conn = connect()
    try:
        with write_txn(conn):
            conn.execute(
                "INSERT INTO batches (id, revision, max_matching_size, bank_count,"
                " system_count, created_at, state_json) VALUES (?,?,?,?,?,?,?)",
                (
                    batch_id,
                    1,
                    max_size,
                    len(bank_rows),
                    len(system_rows),
                    created_at,
                    json.dumps(state, ensure_ascii=False),
                ),
            )
            conn.executemany(
                "INSERT INTO bank_rows (batch_id, row_id, rdate, currency, amount_cents)"
                " VALUES (?,?,?,?,?)",
                [
                    (batch_id, r["row_id"], r["rdate"], r["currency"], r["amount_cents"])
                    for r in bank_rows
                ],
            )
            conn.executemany(
                "INSERT INTO system_rows (batch_id, row_id, rdate, currency, amount_cents)"
                " VALUES (?,?,?,?,?)",
                [
                    (batch_id, r["row_id"], r["rdate"], r["currency"], r["amount_cents"])
                    for r in system_rows
                ],
            )
    finally:
        conn.close()
    return state


@app.get("/batches", response_model=list[BatchSummary], summary="列出全部对账批次")
def list_batches():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, revision, max_matching_size, bank_count, system_count, created_at"
            " FROM batches ORDER BY created_at, id"
        ).fetchall()
        return [
            {
                "batch_id": r["id"],
                "revision": r["revision"],
                "max_matching_size": r["max_matching_size"],
                "bank_count": r["bank_count"],
                "system_count": r["system_count"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    finally:
        conn.close()


@app.get("/batches/{batch_id}", response_model=BatchState, summary="查询批次当前一致状态")
def get_batch(batch_id: str):
    conn = connect()
    try:
        row = conn.execute(
            "SELECT state_json FROM batches WHERE id = ?", (batch_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "batch_not_found"})
    return json.loads(row["state_json"])


@app.post(
    "/batches/{batch_id}/locks",
    response_model=BatchState,
    summary="人工锁定一对配对（携带所见修订号）",
    responses={
        409: {"description": "修订号过期，状态未变"},
        422: {"description": "锁定不可行（非候选配对/行已锁定/无法保持最大配对数），状态未变"},
    },
)
def lock_pair(batch_id: str, req: LockRequest):
    conn = connect()
    try:
        # 1) 一致性快照读取（不持写锁）
        with snapshot(conn):
            loaded = load_batch_inputs(conn, batch_id)
        if loaded is None:
            raise HTTPException(status_code=404, detail={"error": "batch_not_found"})
        batch, bank_rows, system_rows, locks = loaded
        revision = batch["revision"]

        # 2) 基于快照校验与计算（不进事务、不持锁）
        if req.expected_revision != revision:
            raise _conflict(revision)

        left_ids = {r["row_id"] for r in bank_rows}
        right_ids = {r["row_id"] for r in system_rows}
        if req.left_id not in left_ids or req.right_id not in right_ids:
            raise HTTPException(
                status_code=422,
                detail={"error": "unknown_row_id", "message": "配对引用了不存在的行"},
            )
        locked_left = {l for l, _ in locks}
        locked_right = {r for _, r in locks}
        if req.left_id in locked_left or req.right_id in locked_right:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "already_locked",
                    "message": "配对涉及的行已被锁定，每行最多锁定一次",
                },
            )
        idx_left = {rid: i for i, rid in enumerate(r["row_id"] for r in bank_rows)}
        idx_right = {rid: i for i, rid in enumerate(r["row_id"] for r in system_rows)}
        candidate = (idx_left[req.left_id], idx_right[req.right_id])
        if candidate not in set(build_edges(bank_rows, system_rows)):
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "not_a_candidate_pair",
                    "message": "该配对不满足匹配规则（币种/金额相同且日期差≤2天）",
                },
            )

        new_locks = locks + [(req.left_id, req.right_id)]
        analysis, feasible, max_size = compute_analysis(bank_rows, system_rows, new_locks)
        if not feasible:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "lock_infeasible",
                    "message": "锁定该配对后将无法达到原最大配对数，已拒绝并保留原状",
                    "max_matching_size": max_size,
                },
            )

        new_revision = revision + 1
        state = _build_state(
            batch_id,
            new_revision,
            batch["created_at"],
            bank_rows,
            system_rows,
            new_locks,
            analysis,
        )

        # 3) 短写事务：条件更新携带所见修订号，锁定+修订+分析结果一并生效
        try:
            with write_txn(conn):
                cur = conn.execute(
                    "UPDATE batches SET revision = ?, max_matching_size = ?, state_json = ?"
                    " WHERE id = ? AND revision = ?",
                    (
                        new_revision,
                        max_size,
                        json.dumps(state, ensure_ascii=False),
                        batch_id,
                        revision,
                    ),
                )
                if cur.rowcount == 0:
                    raise _StaleRevision
                conn.execute(
                    "INSERT INTO locks (batch_id, left_id, right_id, created_at)"
                    " VALUES (?,?,?,?)",
                    (batch_id, req.left_id, req.right_id, _now()),
                )
        except _StaleRevision:
            raise _conflict(_current_revision(conn, batch_id))
        return state
    finally:
        conn.close()


@app.post(
    "/batches/{batch_id}/unlocks",
    response_model=BatchState,
    summary="撤销人工锁定（携带所见修订号）",
    responses={
        404: {"description": "批次或锁定不存在"},
        409: {"description": "修订号过期，状态未变"},
    },
)
def unlock_pair(batch_id: str, req: UnlockRequest):
    conn = connect()
    try:
        with snapshot(conn):
            loaded = load_batch_inputs(conn, batch_id)
        if loaded is None:
            raise HTTPException(status_code=404, detail={"error": "batch_not_found"})
        batch, bank_rows, system_rows, locks = loaded
        revision = batch["revision"]

        if req.expected_revision != revision:
            raise _conflict(revision)

        new_locks = [(l, r) for l, r in locks if l != req.left_id]
        if len(new_locks) == len(locks):
            raise HTTPException(
                status_code=404,
                detail={"error": "lock_not_found", "message": "该行没有已生效的锁定"},
            )

        analysis, feasible, max_size = compute_analysis(bank_rows, system_rows, new_locks)
        assert feasible  # 减少锁定约束不会破坏最大配对数

        new_revision = revision + 1
        state = _build_state(
            batch_id,
            new_revision,
            batch["created_at"],
            bank_rows,
            system_rows,
            new_locks,
            analysis,
        )

        try:
            with write_txn(conn):
                cur = conn.execute(
                    "UPDATE batches SET revision = ?, max_matching_size = ?, state_json = ?"
                    " WHERE id = ? AND revision = ?",
                    (
                        new_revision,
                        max_size,
                        json.dumps(state, ensure_ascii=False),
                        batch_id,
                        revision,
                    ),
                )
                if cur.rowcount == 0:
                    raise _StaleRevision
                conn.execute(
                    "DELETE FROM locks WHERE batch_id = ? AND left_id = ?",
                    (batch_id, req.left_id),
                )
        except _StaleRevision:
            raise _conflict(_current_revision(conn, batch_id))
        return state
    finally:
        conn.close()
