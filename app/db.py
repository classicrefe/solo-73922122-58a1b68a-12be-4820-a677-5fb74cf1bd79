# -*- coding: utf-8 -*-
"""SQLite 持久化：炉次账（账套/炉/出货批/转料）、签单与核定结果。"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Tuple

DB_PATH = os.environ.get(
    "AUDIT_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "audit.db")
)
DB_PATH = os.path.abspath(DB_PATH)

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS ledgers (
    id INTEGER PRIMARY KEY,
    filename TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    is_current INTEGER NOT NULL DEFAULT 0,
    note TEXT
);

CREATE TABLE IF NOT EXISTS furnaces (
    id INTEGER PRIMARY KEY,
    ledger_id INTEGER NOT NULL REFERENCES ledgers(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    primary_kg INTEGER NOT NULL CHECK (primary_kg >= 0),
    recycled_kg INTEGER NOT NULL CHECK (recycled_kg >= 0),
    UNIQUE (ledger_id, code)
);

CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY,
    ledger_id INTEGER NOT NULL REFERENCES ledgers(id) ON DELETE CASCADE,
    furnace_code TEXT NOT NULL,
    code TEXT NOT NULL,
    weight_kg INTEGER NOT NULL CHECK (weight_kg >= 0),
    UNIQUE (ledger_id, code)
);

CREATE TABLE IF NOT EXISTS transfers (
    id INTEGER PRIMARY KEY,
    ledger_id INTEGER NOT NULL REFERENCES ledgers(id) ON DELETE CASCADE,
    src_furnace TEXT NOT NULL,
    dst_furnace TEXT NOT NULL,
    weight_kg INTEGER NOT NULL CHECK (weight_kg >= 0),
    CHECK (src_furnace <> dst_furnace)
);

-- 签单按出货批号签发；每个批号至多一张有效（active=1）签单。
-- 签单不绑账套：换账后批号仍在则继续生效，批号消失则新账无法核定。
CREATE TABLE IF NOT EXISTS certificates (
    id INTEGER PRIMARY KEY,
    batch_code TEXT NOT NULL,
    min_recycled_kg INTEGER NOT NULL CHECK (min_recycled_kg > 0),
    active INTEGER NOT NULL DEFAULT 1,
    signed_at TEXT NOT NULL,
    revoked_at TEXT
);
-- 每个批号至多一张有效签单（撤销后的历史记录不受限制）。
CREATE UNIQUE INDEX IF NOT EXISTS ux_cert_active_batch
    ON certificates(batch_code) WHERE active = 1;

-- 每个账套最近一次核定结果（成功才落库，失败保留旧记录）。
CREATE TABLE IF NOT EXISTS solutions (
    id INTEGER PRIMARY KEY,
    ledger_id INTEGER NOT NULL UNIQUE REFERENCES ledgers(id) ON DELETE CASCADE,
    ok INTEGER NOT NULL,
    reason TEXT,
    solved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS solution_batches (
    id INTEGER PRIMARY KEY,
    solution_id INTEGER NOT NULL REFERENCES solutions(id) ON DELETE CASCADE,
    batch_code TEXT NOT NULL,
    recycled_kg INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS solution_transfers (
    id INTEGER PRIMARY KEY,
    solution_id INTEGER NOT NULL REFERENCES solutions(id) ON DELETE CASCADE,
    transfer_id INTEGER NOT NULL,
    recycled_kg INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL,
    success INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_batches_ledger ON batches(ledger_id);
CREATE INDEX IF NOT EXISTS idx_transfers_ledger ON transfers(ledger_id);
CREATE INDEX IF NOT EXISTS idx_cert_active ON certificates(active);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def log_event(conn: sqlite3.Connection, action: str, detail: str, success: bool) -> None:
    conn.execute(
        "INSERT INTO audit_log(ts, action, detail, success) VALUES (?, ?, ?, ?)",
        (utcnow_iso(), action, detail, 1 if success else 0),
    )


def current_ledger_id(conn: sqlite3.Connection) -> Optional[int]:
    row = conn.execute(
        "SELECT id FROM ledgers WHERE is_current = 1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["id"] if row else None


def load_ledger(
    conn: sqlite3.Connection, ledger_id: int
) -> Tuple[List[sqlite3.Row], List[sqlite3.Row], List[sqlite3.Row]]:
    furnaces = conn.execute(
        "SELECT * FROM furnaces WHERE ledger_id = ? ORDER BY code", (ledger_id,)
    ).fetchall()
    batches = conn.execute(
        "SELECT * FROM batches WHERE ledger_id = ? ORDER BY code", (ledger_id,)
    ).fetchall()
    transfers = conn.execute(
        "SELECT * FROM transfers WHERE ledger_id = ? ORDER BY id", (ledger_id,)
    ).fetchall()
    return furnaces, batches, transfers


def active_certificates(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM certificates WHERE active = 1 ORDER BY batch_code"
    ).fetchall()


def save_solution(
    conn: sqlite3.Connection,
    ledger_id: int,
    ok: bool,
    reason: Optional[str],
    batch_values: Iterable[Tuple[str, int]] = (),
    transfer_values: Iterable[Tuple[int, int]] = (),
) -> int:
    """覆盖式保存某账套的核定结果（调用方已在事务中）。"""
    old = conn.execute(
        "SELECT id FROM solutions WHERE ledger_id = ?", (ledger_id,)
    ).fetchone()
    if old:
        conn.execute("DELETE FROM solutions WHERE id = ?", (old["id"],))
    cur = conn.execute(
        "INSERT INTO solutions(ledger_id, ok, reason, solved_at) VALUES (?, ?, ?, ?)",
        (ledger_id, 1 if ok else 0, reason, utcnow_iso()),
    )
    sid = cur.lastrowid
    conn.executemany(
        "INSERT INTO solution_batches(solution_id, batch_code, recycled_kg) "
        "VALUES (?, ?, ?)",
        [(sid, b, v) for b, v in batch_values],
    )
    conn.executemany(
        "INSERT INTO solution_transfers(solution_id, transfer_id, recycled_kg) "
        "VALUES (?, ?, ?)",
        [(sid, t, v) for t, v in transfer_values],
    )
    return sid


def get_solution(conn: sqlite3.Connection, ledger_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM solutions WHERE ledger_id = ?", (ledger_id,)
    ).fetchone()


def solution_details(
    conn: sqlite3.Connection, solution_id: int
) -> Tuple[List[sqlite3.Row], List[sqlite3.Row]]:
    sb = conn.execute(
        "SELECT * FROM solution_batches WHERE solution_id = ? ORDER BY batch_code",
        (solution_id,),
    ).fetchall()
    st = conn.execute(
        "SELECT * FROM solution_transfers WHERE solution_id = ? ORDER BY transfer_id",
        (solution_id,),
    ).fetchall()
    return sb, st
