"""Persistent state: dedup ledger + draft store (replaces n8n staticData).

SQLite at ~/.local/share/gmail-bot/state.db.
- processed: (msg id, ts) with a 48h TTL, pruned on each poll, capped at 300.
- drafts: msgId -> draft JSON, capped at 50 (oldest by created_at dropped).
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "gmail-bot" / "state.db"

PROCESSED_TTL_MS = 48 * 3600 * 1000
PROCESSED_CAP = 300
DRAFTS_CAP = 50


@dataclass
class Draft:
    text: str
    thread_id: str
    last_msg_id: str
    chat_id: int
    tg_message_id: int
    created_at: int  # epoch ms


def _now_ms() -> int:
    return int(time.time() * 1000)


class State:
    """SQLite-backed dedup + draft store."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        if db_path != Path(":memory:"):
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS processed (
                id TEXT PRIMARY KEY,
                ts INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS drafts (
                msg_id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---- dedup ----------------------------------------------------------

    def is_processed(self, msg_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM processed WHERE id = ?", (msg_id,)
        ).fetchone()
        return row is not None

    def mark_processed(self, msg_id: str, ts: int | None = None) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO processed (id, ts) VALUES (?, ?)",
            (msg_id, ts if ts is not None else _now_ms()),
        )
        self._conn.commit()
        self._enforce_processed_cap()

    def prune_processed(self, now_ms: int | None = None) -> int:
        """Delete entries older than the 48h TTL. Returns rows deleted."""
        cutoff = (now_ms if now_ms is not None else _now_ms()) - PROCESSED_TTL_MS
        cur = self._conn.execute("DELETE FROM processed WHERE ts < ?", (cutoff,))
        self._conn.commit()
        return cur.rowcount

    def _enforce_processed_cap(self) -> None:
        """Keep only the newest PROCESSED_CAP entries."""
        self._conn.execute(
            """
            DELETE FROM processed
            WHERE id NOT IN (
                SELECT id FROM processed ORDER BY ts DESC LIMIT ?
            )
            """,
            (PROCESSED_CAP,),
        )
        self._conn.commit()

    def processed_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM processed").fetchone()[0]

    # ---- drafts ---------------------------------------------------------

    def save_draft(self, msg_id: str, draft: Draft) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO drafts (msg_id, data, created_at) VALUES (?, ?, ?)",
            (msg_id, json.dumps(asdict(draft)), draft.created_at),
        )
        self._conn.commit()
        self._enforce_drafts_cap()

    def get_draft(self, msg_id: str) -> Draft | None:
        row = self._conn.execute(
            "SELECT data FROM drafts WHERE msg_id = ?", (msg_id,)
        ).fetchone()
        if row is None:
            return None
        return Draft(**json.loads(row["data"]))

    def _enforce_drafts_cap(self) -> None:
        """Drop oldest drafts by created_at beyond DRAFTS_CAP."""
        self._conn.execute(
            """
            DELETE FROM drafts
            WHERE msg_id NOT IN (
                SELECT msg_id FROM drafts ORDER BY created_at DESC LIMIT ?
            )
            """,
            (DRAFTS_CAP,),
        )
        self._conn.commit()

    def drafts_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0]
