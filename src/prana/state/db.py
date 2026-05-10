"""SQLite WAL connection + schema for ~/.narada/state.db."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


NARADA_ROOT = Path.home() / ".narada"
NARADA_STATE_DB = NARADA_ROOT / "state.db"


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS utterance_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,                  -- ISO8601 UTC
    source          TEXT NOT NULL,                  -- 'heartbeat-speak', 'heartbeat-check-in', etc
    topic           TEXT NOT NULL DEFAULT '',
    text            TEXT NOT NULL,
    priority        INTEGER NOT NULL DEFAULT 0,     -- higher = more urgent
    delivered_at    TEXT,                           -- NULL = pending
    delivered_to    TEXT,                           -- 'body' | 'telegram:<chat_id>' | 'skipped:<reason>'
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT
);

CREATE INDEX IF NOT EXISTS idx_utterance_pending
    ON utterance_queue(delivered_at, priority DESC, id ASC)
    WHERE delivered_at IS NULL;

-- Schema version for future migrations
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_version (version, applied_at)
    VALUES (1, datetime('now'));
"""


def get_db(path: Path = NARADA_STATE_DB) -> sqlite3.Connection:
    """Open (or create) state.db with WAL mode + foreign keys.

    WAL mode lets the heartbeat write while a drainer or smoke test
    reads concurrently without blocking. The connection is configured
    to autocommit on every statement for cross-process safety.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        timeout=10.0,
        isolation_level=None,  # autocommit; explicit BEGIN/COMMIT when needed
        check_same_thread=False,
    )
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path: Path = NARADA_STATE_DB) -> None:
    """Create tables if missing. Safe to call repeatedly."""
    conn = get_db(path)
    try:
        conn.executescript(SCHEMA_SQL)
        logger.debug("state.db initialized at %s", path)
    finally:
        conn.close()
