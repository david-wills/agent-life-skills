"""SQLite helpers shared by skills that keep state in a database.

``connect_db`` opens a file in WAL mode with row access by name. The
``skill_state`` table is a small key/value store for cursors and flags; every
skill that uses it calls ``ensure_state_table`` first, so a fresh database works.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from typing import Any


def connect_db(db_path: pathlib.Path | str) -> sqlite3.Connection:
    db_path = pathlib.Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_state_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS skill_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )


def get_state(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    ensure_state_table(conn)
    row = conn.execute("SELECT value FROM skill_state WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return row["value"]


def set_state(conn: sqlite3.Connection, key: str, value: Any) -> None:
    ensure_state_table(conn)
    conn.execute(
        "INSERT INTO skill_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )
