"""SQLite 持久化层。

设计要点：
  * WAL 模式 + busy_timeout：两个 API 进程共库时读写不互相阻塞；
  * 计算（匹配分析）在事务外进行，不持有任何数据库锁；
  * 写事务极短：条件 UPDATE 携带所见修订号实现乐观并发控制，
    锁定记录、修订号与分析结果在同一事务内一并生效；
  * 读接口只取单行 state_json（WAL 快照），保证不混用状态；
  * 数据库文件落在磁盘卷上，进程/容器重启后状态保留。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id                 TEXT PRIMARY KEY,
    revision           INTEGER NOT NULL,
    max_matching_size  INTEGER NOT NULL,
    bank_count         INTEGER NOT NULL,
    system_count       INTEGER NOT NULL,
    created_at         TEXT NOT NULL,
    state_json         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bank_rows (
    batch_id     TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    row_id       TEXT NOT NULL,
    rdate        TEXT NOT NULL,
    currency     TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    PRIMARY KEY (batch_id, row_id)
);
CREATE TABLE IF NOT EXISTS system_rows (
    batch_id     TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    row_id       TEXT NOT NULL,
    rdate        TEXT NOT NULL,
    currency     TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    PRIMARY KEY (batch_id, row_id)
);
CREATE TABLE IF NOT EXISTS locks (
    batch_id   TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    left_id    TEXT NOT NULL,
    right_id   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, left_id)
);
"""


def db_path() -> str:
    return os.environ.get("RECON_DB", os.path.join(os.getcwd(), "recon.db"))


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    path = db_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def snapshot(conn: sqlite3.Connection):
    """只读快照事务：固定一个一致性视图，不阻塞任何写方。"""
    conn.execute("BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


@contextmanager
def write_txn(conn: sqlite3.Connection):
    """短写事务：BEGIN IMMEDIATE 序列化写方，乐观修订检查在其中完成。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def load_batch_inputs(conn: sqlite3.Connection, batch_id: str):
    """在调用方的事务内读取计算所需的全部输入。"""
    batch = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if batch is None:
        return None
    bank_rows = [
        dict(r)
        for r in conn.execute(
            "SELECT row_id, rdate, currency, amount_cents FROM bank_rows"
            " WHERE batch_id = ? ORDER BY rowid",
            (batch_id,),
        )
    ]
    system_rows = [
        dict(r)
        for r in conn.execute(
            "SELECT row_id, rdate, currency, amount_cents FROM system_rows"
            " WHERE batch_id = ? ORDER BY rowid",
            (batch_id,),
        )
    ]
    locks = [
        (r["left_id"], r["right_id"])
        for r in conn.execute(
            "SELECT left_id, right_id FROM locks WHERE batch_id = ? ORDER BY rowid",
            (batch_id,),
        )
    ]
    return batch, bank_rows, system_rows, locks
