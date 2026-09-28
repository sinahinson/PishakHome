"""
Pishak Home — database layer.

A thin, dependency-free (stdlib sqlite3) wrapper. Kept intentionally simple:
the Raspberry Pi 3B+ doesn't need a connection pool or an ORM for this
workload, and a plain module-level connection with WAL mode is both fast
enough and easy to reason about.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    event_type TEXT NOT NULL,
    camera_id TEXT,
    zone TEXT,
    confidence REAL,
    snapshot_path TEXT,
    recording_path TEXT,
    meta TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);

CREATE TABLE IF NOT EXISTS recordings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    ended_at REAL,
    path TEXT NOT NULL,
    trigger TEXT NOT NULL DEFAULT 'manual',
    size_bytes INTEGER
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    path TEXT NOT NULL,
    event_id INTEGER,
    FOREIGN KEY(event_id) REFERENCES events(id)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS system_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
"""


class Database:
    """Owns one sqlite3 connection and exposes small, explicit helpers.

    A class (rather than bare module-level globals) so tests can create an
    isolated in-memory or temp-file instance without stepping on the
    application's real database.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # One shared connection is used from many threads (web requests,
        # the motion loop, the retention loop, startup code). sqlite3
        # connections are NOT safe for concurrent use, so every operation
        # is serialized through this lock. Without it, concurrent access
        # produced "bad parameter or other API misuse" / "cannot commit -
        # no transaction is active" errors, sometimes even at startup.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- events -----------------------------------------------------

    def add_event(
        self,
        event_type: str,
        camera_id: Optional[str] = None,
        zone: Optional[str] = None,
        confidence: Optional[float] = None,
        snapshot_path: Optional[str] = None,
        recording_path: Optional[str] = None,
        meta: Optional[dict[str, Any]] = None,
        timestamp: Optional[float] = None,
    ) -> int:
        ts = timestamp if timestamp is not None else time.time()
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO events
                   (timestamp, event_type, camera_id, zone, confidence,
                    snapshot_path, recording_path, meta)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ts,
                    event_type,
                    camera_id,
                    zone,
                    confidence,
                    snapshot_path,
                    recording_path,
                    json.dumps(meta) if meta else None,
                ),
            )
            return cur.lastrowid

    def get_events(
        self, limit: int = 50, event_type: Optional[str] = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM events"
        params: list[Any] = []
        if event_type:
            query += " WHERE event_type = ?"
            params.append(event_type)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        with self.cursor() as cur:
            cur.execute(query, params)
            rows = [dict(row) for row in cur.fetchall()]
        for row in rows:
            if row.get("meta"):
                try:
                    row["meta"] = json.loads(row["meta"])
                except (TypeError, json.JSONDecodeError):
                    pass
        return rows

    def clear_events(self) -> int:
        with self.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM events")
            count = cur.fetchone()["n"]
            cur.execute("DELETE FROM events")
        return count

    # ---- recordings ---------------------------------------------------

    def start_recording(self, path: str, trigger: str = "manual") -> int:
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO recordings (started_at, path, trigger) VALUES (?, ?, ?)",
                (time.time(), path, trigger),
            )
            return cur.lastrowid

    def finish_recording(self, recording_id: int, size_bytes: Optional[int] = None) -> None:
        with self.cursor() as cur:
            cur.execute(
                "UPDATE recordings SET ended_at = ?, size_bytes = ? WHERE id = ?",
                (time.time(), size_bytes, recording_id),
            )

    def get_recordings(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM recordings ORDER BY started_at DESC LIMIT ?", (limit,)
            )
            return [dict(row) for row in cur.fetchall()]

    # ---- snapshots ------------------------------------------------------

    def add_snapshot(self, path: str, event_id: Optional[int] = None) -> int:
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO snapshots (timestamp, path, event_id) VALUES (?, ?, ?)",
                (time.time(), path, event_id),
            )
            return cur.lastrowid

    # ---- settings (simple key/value store) -----------------------------

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cur.fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            return row["value"]

    def set_setting(self, key: str, value: Any) -> None:
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )

    # ---- system events (errors/warnings for later inspection) ----------

    def log_system_event(self, level: str, message: str) -> None:
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO system_events (timestamp, level, message) VALUES (?, ?, ?)",
                (time.time(), level, message),
            )
