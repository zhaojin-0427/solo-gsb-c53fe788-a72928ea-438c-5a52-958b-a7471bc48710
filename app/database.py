"""SQLite 持久化：路网版本、封闭时段、核验记录。

核验记录把当时的路网快照与封闭时段快照一并固化（JSON 文本），
之后导入新路网或修改封闭时段都不会改变旧结果。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

DB_PATH = os.environ.get("DATABASE_PATH", "/data/accessibility.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS network_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    payload TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS closures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    network_id INTEGER NOT NULL REFERENCES network_versions(id) ON DELETE CASCADE,
    edge_id TEXT NOT NULL,
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    raw_start TEXT NOT NULL,
    raw_end TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verification_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    network_id INTEGER NOT NULL,
    network_name TEXT NOT NULL,
    network_snapshot TEXT NOT NULL,
    closures_snapshot TEXT NOT NULL,
    params TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(_SCHEMA)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")


# ---------------------------------------------------------------- 路网版本

def create_network_version(payload: Dict[str, Any]) -> int:
    with connect() as conn:
        conn.execute("UPDATE network_versions SET is_active = 0")
        cur = conn.execute(
            "INSERT INTO network_versions(name, payload, imported_at, is_active)"
            " VALUES (?, ?, ?, 1)",
            (payload["name"], json.dumps(payload, ensure_ascii=False), now_iso()))
        return int(cur.lastrowid)


def get_active_network() -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM network_versions WHERE is_active = 1"
            " ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        return _network_row(row)


def get_network(network_id: int) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM network_versions WHERE id = ?",
            (network_id,)).fetchone()
        return _network_row(row) if row else None


def list_networks() -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, name, imported_at, is_active, payload"
            " FROM network_versions ORDER BY id DESC").fetchall()
    out = []
    for r in rows:
        payload = json.loads(r["payload"])
        out.append({"id": r["id"], "name": r["name"],
                    "imported_at": r["imported_at"],
                    "is_active": bool(r["is_active"]),
                    "floors": payload.get("floors", []),
                    "node_count": len(payload.get("nodes", [])),
                    "edge_count": len(payload.get("edges", []))})
    return out


def set_active_network(network_id: int) -> bool:
    with connect() as conn:
        row = conn.execute("SELECT 1 FROM network_versions WHERE id = ?",
                           (network_id,)).fetchone()
        if not row:
            return False
        conn.execute("UPDATE network_versions SET is_active = 0")
        conn.execute("UPDATE network_versions SET is_active = 1 WHERE id = ?",
                     (network_id,))
        return True


def _network_row(row: sqlite3.Row) -> Dict[str, Any]:
    payload = json.loads(row["payload"])
    return {"id": row["id"], "name": row["name"],
            "imported_at": row["imported_at"],
            "is_active": bool(row["is_active"]), "payload": payload}


# ---------------------------------------------------------------- 封闭时段

def list_closures(network_id: int) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM closures WHERE network_id = ? ORDER BY id",
            (network_id,)).fetchall()
    return [dict(r) for r in rows]


def add_closure(network_id: int, edge_id: str, start_utc: str, end_utc: str,
                raw_start: str, raw_end: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO closures(network_id, edge_id, start_utc, end_utc,"
            " raw_start, raw_end, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (network_id, edge_id, start_utc, end_utc,
             raw_start, raw_end, now_iso()))
        return int(cur.lastrowid)


def delete_closure(network_id: int, closure_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM closures WHERE id = ? AND network_id = ?",
            (closure_id, network_id))
        return cur.rowcount > 0


# ---------------------------------------------------------------- 核验记录

def save_record(network_id: int, network_name: str,
                network_snapshot: Dict[str, Any],
                closures_snapshot: List[Dict[str, Any]],
                params: Dict[str, Any], result: Dict[str, Any]) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO verification_records(network_id, network_name,"
            " network_snapshot, closures_snapshot, params, result, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (network_id, network_name,
             json.dumps(network_snapshot, ensure_ascii=False),
             json.dumps(closures_snapshot, ensure_ascii=False),
             json.dumps(params, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False), now_iso()))
        return int(cur.lastrowid)


def list_records(limit: int = 100) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, network_id, network_name, created_at, params, result"
            " FROM verification_records ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
    out = []
    for r in rows:
        params = json.loads(r["params"])
        result = json.loads(r["result"])
        out.append({
            "id": r["id"], "network_id": r["network_id"],
            "network_name": r["network_name"], "created_at": r["created_at"],
            "start": params.get("start"), "goal": params.get("goal"),
            "departure": params.get("departure"),
            "min_clear_width": params.get("min_clear_width", 0),
            "max_slope": params.get("max_slope", 0),
            "reachable": bool(result.get("reachable")),
        })
    return out


def get_record(record_id: int) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM verification_records WHERE id = ?",
            (record_id,)).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"], "network_id": row["network_id"],
        "network_name": row["network_name"], "created_at": row["created_at"],
        "network_snapshot": json.loads(row["network_snapshot"]),
        "closures_snapshot": json.loads(row["closures_snapshot"]),
        "params": json.loads(row["params"]),
        "result": json.loads(row["result"]),
    }
