from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import settings
from .security import validate_channel_name, validate_output_folder


SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 0,
    input_url TEXT NOT NULL,
    output_folder TEXT NOT NULL DEFAULT '',
    segment_duration INTEGER NOT NULL DEFAULT 300,
    automatic_recording INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_channels_enabled ON channels(enabled, automatic_recording);
"""


class Database:
    def __init__(self, path: Path = settings.database_path):
        self.path = path
        self._lock = threading.RLock()

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    def list_channels(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM channels ORDER BY display_name").fetchall()
        return [dict(row) for row in rows]

    def get_channel(self, channel_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
        return dict(row) if row else None

    def create_channel(self, data: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        payload = self._clean_payload(data)
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO channels
                (display_name, enabled, input_url, output_folder, segment_duration,
                 automatic_recording, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["display_name"],
                    int(payload["enabled"]),
                    payload["input_url"],
                    payload["output_folder"],
                    payload["segment_duration"],
                    int(payload["automatic_recording"]),
                    now,
                    now,
                ),
            )
            channel_id = cur.lastrowid
        return self.get_channel(int(channel_id))  # type: ignore[arg-type]

    def update_channel(self, channel_id: int, data: dict[str, Any]) -> dict[str, Any] | None:
        existing = self.get_channel(channel_id)
        if not existing:
            return None
        merged = {**existing, **data}
        payload = self._clean_payload(merged)
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE channels
                SET display_name = ?, enabled = ?, input_url = ?, output_folder = ?,
                    segment_duration = ?, automatic_recording = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    payload["display_name"],
                    int(payload["enabled"]),
                    payload["input_url"],
                    payload["output_folder"],
                    payload["segment_duration"],
                    int(payload["automatic_recording"]),
                    now,
                    channel_id,
                ),
            )
        return self.get_channel(channel_id)

    def delete_channel(self, channel_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
        return cur.rowcount > 0

    def _clean_payload(self, data: dict[str, Any]) -> dict[str, Any]:
        duration = int(data.get("segment_duration") or 300)
        if duration < 30 or duration > 86400:
            raise ValueError("Segment duration must be between 30 and 86400 seconds.")
        input_url = str(data.get("input_url") or "").strip()
        if not input_url:
            raise ValueError("Input URL is required.")
        return {
            "display_name": validate_channel_name(str(data.get("display_name") or "")),
            "enabled": bool(data.get("enabled")),
            "input_url": input_url,
            "output_folder": validate_output_folder(str(data.get("output_folder") or "")),
            "segment_duration": duration,
            "automatic_recording": bool(data.get("automatic_recording")),
        }


db = Database()
