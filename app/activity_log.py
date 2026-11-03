"""Persistent, authenticated activity history for the control plane."""

import sqlite3
import threading
from pathlib import Path

from app.config import settings

_lock = threading.Lock()


def _connect():
    path = Path(settings.BACKUP_STORAGE_DIR) / "activity.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS activity_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT NOT NULL UNIQUE,
            actor TEXT NOT NULL,
            actor_role TEXT NOT NULL,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            db_id TEXT,
            ts INTEGER NOT NULL
        )"""
    )
    # Migration: add db_id column to existing tables that lack it
    try:
        conn.execute("ALTER TABLE activity_events ADD COLUMN db_id TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists
    conn.execute(
        "CREATE TABLE IF NOT EXISTS activity_meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)"
    )
    conn.commit()
    return conn


def _event(row):
    return {
        "id": row["id"],
        "client_id": row["client_id"],
        "actor": row["actor"],
        "actorRole": row["actor_role"],
        "kind": row["kind"],
        "title": row["title"],
        "detail": row["detail"],
        "db_id": row["db_id"],
        "ts": row["ts"],
    }


def record_activity(client_id, actor, actor_role, kind, title, detail, ts, db_id=None):
    with _lock:
        conn = _connect()
        try:
            marker = conn.execute(
                "SELECT value FROM activity_meta WHERE key = 'cleared_at'"
            ).fetchone()
            if marker and ts <= marker[0]:
                return None
            conn.execute(
                """INSERT OR IGNORE INTO activity_events
                   (client_id, actor, actor_role, kind, title, detail, db_id, ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (client_id, actor, actor_role, kind, title, detail, db_id, ts),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM activity_events WHERE client_id = ?", (client_id,)
            ).fetchone()
            return _event(row)
        finally:
            conn.close()


def list_activity(limit=50, db_id=None):
    with _lock:
        conn = _connect()
        try:
            if db_id:
                rows = conn.execute(
                    "SELECT * FROM activity_events WHERE db_id = ? ORDER BY ts DESC, id DESC LIMIT ?",
                    (db_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM activity_events ORDER BY ts DESC, id DESC LIMIT ?", (limit,)
                ).fetchall()
            marker = conn.execute(
                "SELECT value FROM activity_meta WHERE key = 'cleared_at'"
            ).fetchone()
            return [_event(row) for row in rows], (marker[0] if marker else 0)
        finally:
            conn.close()


def clear_activity():
    import time

    cleared_at = int(time.time() * 1000)
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM activity_events")
            conn.execute(
                "INSERT OR REPLACE INTO activity_meta(key, value) VALUES ('cleared_at', ?)",
                (cleared_at,),
            )
            conn.commit()
        finally:
            conn.close()
    return cleared_at
