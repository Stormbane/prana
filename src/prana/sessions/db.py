"""SQLite WAL connection + schema for ~/.narada/sessions.db."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

NARADA_ROOT = Path.home() / ".narada"
SESSIONS_DB = NARADA_ROOT / "sessions.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,               -- our uuid
    provider        TEXT NOT NULL,                  -- 'claude' | 'codex' | 'kimi'
    provider_session_id TEXT,                       -- e.g. claude session uuid (for --resume)
    cwd             TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    state           TEXT NOT NULL,                  -- SessionState value
    pid             INTEGER,
    pane_id         TEXT,                           -- wezterm pane id, if mirrored
    idempotency_key TEXT,                           -- dedupe spawn retries
    created_at      TEXT NOT NULL,                  -- ISO8601 UTC
    updated_at      TEXT NOT NULL,
    last_activity_at TEXT,
    ended_at        TEXT,
    exit_code       INTEGER,
    last_error      TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_idempotency
    ON sessions(idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_sessions_live
    ON sessions(state) WHERE state IN ('spawning', 'running', 'idle');

-- Proposals: mutations requested by an unprivileged caller (voice tier),
-- awaiting judgment from the prana tier. The sovereignty boundary's
-- durable queue.
CREATE TABLE IF NOT EXISTS proposals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    caller          TEXT NOT NULL,                  -- tier name of the requester
    tool            TEXT NOT NULL,                  -- e.g. 'spawn_session'
    params_json     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',-- pending|approved|rejected|executed|expired
    decided_at      TEXT,
    decided_by      TEXT,
    decision_reason TEXT,
    capability      TEXT,                           -- single-use token issued on approval
    executed_at     TEXT
);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_version (version, applied_at)
    VALUES (1, datetime('now'));
"""


def get_db(path: Path = SESSIONS_DB) -> sqlite3.Connection:
    """Open (or create) sessions.db with WAL mode. Same pattern as state.db."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        timeout=10.0,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path: Path = SESSIONS_DB) -> None:
    """Create tables if missing. Safe to call repeatedly."""
    conn = get_db(path)
    try:
        conn.executescript(SCHEMA_SQL)
        logger.debug("sessions.db initialized at %s", path)
    finally:
        conn.close()
