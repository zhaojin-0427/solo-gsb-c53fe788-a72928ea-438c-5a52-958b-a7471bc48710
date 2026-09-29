"""SQLite persistence for the accessible path verification platform.

Stores network versions, closure periods, and verification records.
Each verification record snapshots the network version and closure set so
that later modifications never alter historical results.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "app.db"

_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with _lock, get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS networks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version INTEGER NOT NULL UNIQUE,
                data TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS closures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                network_version INTEGER NOT NULL,
                edge_id TEXT NOT NULL,
                start TEXT NOT NULL,
                end TEXT NOT NULL,
                tz TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_closures_nv
                ON closures(network_version);

            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                network_version INTEGER NOT NULL,
                closure_version INTEGER NOT NULL,
                closure_snapshot TEXT NOT NULL,
                query TEXT NOT NULL,
                result TEXT NOT NULL,
                reachable INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )


# ---------------------------------------------------------------------------
# Networks
# ---------------------------------------------------------------------------

def insert_network(data: dict[str, Any]) -> int:
    """Insert a new network version. Returns the new version number."""
    with _lock, get_conn() as conn:
        row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM networks").fetchone()
        version = int(row["v"]) + 1
        conn.execute(
            "INSERT INTO networks (version, data, created_at) VALUES (?, ?, ?)",
            (version, json.dumps(data, ensure_ascii=False), _now_iso()),
        )
        # Carry over closures from the previous version whose edge_id still exists.
        prev = version - 1
        if prev > 0:
            edge_ids = {e["id"] for e in data.get("edges", [])}
            old = conn.execute(
                "SELECT edge_id, start, end, tz FROM closures WHERE network_version = ?",
                (prev,),
            ).fetchall()
            for c in old:
                if c["edge_id"] in edge_ids:
                    conn.execute(
                        "INSERT INTO closures (network_version, edge_id, start, end, tz, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (version, c["edge_id"], c["start"], c["end"], c["tz"], _now_iso()),
                    )
        return version


def get_current_network() -> tuple[int, dict[str, Any]] | None:
    with _lock, get_conn() as conn:
        row = conn.execute(
            "SELECT version, data FROM networks ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return int(row["version"]), json.loads(row["data"])


def get_network(version: int) -> dict[str, Any] | None:
    with _lock, get_conn() as conn:
        row = conn.execute(
            "SELECT data FROM networks WHERE version = ?", (version,)
        ).fetchone()
        return json.loads(row["data"]) if row else None


def list_network_versions() -> list[dict[str, Any]]:
    with _lock, get_conn() as conn:
        rows = conn.execute(
            "SELECT version, created_at FROM networks ORDER BY version DESC"
        ).fetchall()
        return [{"version": int(r["version"]), "created_at": r["created_at"]} for r in rows]


# ---------------------------------------------------------------------------
# Closures
# ---------------------------------------------------------------------------

def _next_closure_version(conn: sqlite3.Connection, network_version: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(closure_version), 0) AS v FROM records WHERE network_version = ?",
        (network_version,),
    ).fetchone()
    # Use a monotonic counter derived from a dedicated table; simpler: count
    # closure changes via a separate version table.
    return int(row["v"])


def add_closure(network_version: int, edge_id: str, start: str, end: str, tz: str) -> int:
    with _lock, get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO closures (network_version, edge_id, start, end, tz, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (network_version, edge_id, start, end, tz, _now_iso()),
        )
        return int(cur.lastrowid)


def delete_closure(closure_id: int) -> bool:
    with _lock, get_conn() as conn:
        cur = conn.execute("DELETE FROM closures WHERE id = ?", (closure_id,))
        return cur.rowcount > 0


def list_closures(network_version: int) -> list[dict[str, Any]]:
    with _lock, get_conn() as conn:
        rows = conn.execute(
            "SELECT id, edge_id, start, end, tz, created_at FROM closures "
            "WHERE network_version = ? ORDER BY id",
            (network_version,),
        ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "edge_id": r["edge_id"],
                "start": r["start"],
                "end": r["end"],
                "tz": r["tz"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]


def closure_snapshot(network_version: int) -> tuple[int, list[dict[str, Any]]]:
    """Return (closure_version, snapshot list) for solidifying a record.

    closure_version is a monotonic number that increments whenever the closure
    set for this network version changes. We track it via a dedicated table.
    """
    with _lock, get_conn() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS closure_versions ("
            "network_version INTEGER PRIMARY KEY, version INTEGER NOT NULL)"
        )
        row = conn.execute(
            "SELECT version FROM closure_versions WHERE network_version = ?",
            (network_version,),
        ).fetchone()
        version = int(row["version"]) if row else 0
        rows = conn.execute(
            "SELECT edge_id, start, end, tz FROM closures WHERE network_version = ? ORDER BY id",
            (network_version,),
        ).fetchall()
        snap = [
            {"edge_id": r["edge_id"], "start": r["start"], "end": r["end"], "tz": r["tz"]}
            for r in rows
        ]
        return version, snap


def bump_closure_version(network_version: int) -> int:
    with _lock, get_conn() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS closure_versions ("
            "network_version INTEGER PRIMARY KEY, version INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO closure_versions (network_version, version) VALUES (?, 1) "
            "ON CONFLICT(network_version) DO UPDATE SET version = version + 1",
            (network_version,),
        )
        row = conn.execute(
            "SELECT version FROM closure_versions WHERE network_version = ?",
            (network_version,),
        ).fetchone()
        return int(row["version"])


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

def insert_record(
    network_version: int,
    closure_version: int,
    closure_snap: list[dict[str, Any]],
    query: dict[str, Any],
    result: dict[str, Any],
    reachable: bool,
) -> int:
    with _lock, get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO records (network_version, closure_version, closure_snapshot, query, result, reachable, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                network_version,
                closure_version,
                json.dumps(closure_snap, ensure_ascii=False),
                json.dumps(query, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
                1 if reachable else 0,
                _now_iso(),
            ),
        )
        return int(cur.lastrowid)


def list_records(limit: int = 100) -> list[dict[str, Any]]:
    with _lock, get_conn() as conn:
        rows = conn.execute(
            "SELECT id, network_version, closure_version, reachable, created_at "
            "FROM records ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "network_version": int(r["network_version"]),
                "closure_version": int(r["closure_version"]),
                "reachable": bool(r["reachable"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]


def get_record(record_id: int) -> dict[str, Any] | None:
    with _lock, get_conn() as conn:
        row = conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "network_version": int(row["network_version"]),
            "closure_version": int(row["closure_version"]),
            "closure_snapshot": json.loads(row["closure_snapshot"]),
            "query": json.loads(row["query"]),
            "result": json.loads(row["result"]),
            "reachable": bool(row["reachable"]),
            "created_at": row["created_at"],
        }
