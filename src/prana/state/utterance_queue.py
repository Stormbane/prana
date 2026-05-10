"""Utterance queue — durable log of every thing Narada wanted to say.

The router pushes here BEFORE attempting delivery, then marks the row
delivered_to/delivered_at after the channel succeeds. Failed deliveries
leave the row pending with last_error populated for inspection.

Pending rows can be replayed by a future drainer cron — useful if both
channels were unreachable at utterance time (body off, no internet) and
we want to retry when conditions improve. For now the heartbeat is
synchronous and pending = something went wrong.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from prana.state.db import get_db, init_db

logger = logging.getLogger(__name__)


@dataclass
class Utterance:
    id: int
    created_at: str
    source: str
    topic: str
    text: str
    priority: int
    delivered_at: Optional[str]
    delivered_to: Optional[str]
    attempts: int
    last_error: Optional[str]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def push_utterance(
    text: str,
    *,
    source: str,
    topic: str = "",
    priority: int = 0,
) -> int:
    """Append a pending utterance. Returns its row id."""
    init_db()
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO utterance_queue "
            "(created_at, source, topic, text, priority) "
            "VALUES (?, ?, ?, ?, ?)",
            (_now_iso(), source, topic, text, priority),
        )
        return cur.lastrowid
    finally:
        conn.close()


def mark_delivered(uid: int, channel: str) -> None:
    """Mark row uid as successfully delivered via channel.

    channel: 'body' | 'telegram:<chat_id>' | 'email' | etc.
    """
    conn = get_db()
    try:
        conn.execute(
            "UPDATE utterance_queue "
            "SET delivered_at = ?, delivered_to = ?, attempts = attempts + 1 "
            "WHERE id = ?",
            (_now_iso(), channel, uid),
        )
    finally:
        conn.close()


def mark_skipped(uid: int, reason: str) -> None:
    """Mark a row as not-delivered with an explicit reason. Same shape as
    delivered — sets delivered_at so it doesn't appear in pending() — but
    delivered_to has the 'skipped:<reason>' prefix so audits can find it.
    """
    conn = get_db()
    try:
        conn.execute(
            "UPDATE utterance_queue "
            "SET delivered_at = ?, delivered_to = ?, attempts = attempts + 1, "
            "    last_error = ? "
            "WHERE id = ?",
            (_now_iso(), f"skipped:{reason}", reason, uid),
        )
    finally:
        conn.close()


def mark_failed(uid: int, error: str) -> None:
    """Record a delivery attempt that failed but leave the row pending
    so a future drainer can retry."""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE utterance_queue "
            "SET attempts = attempts + 1, last_error = ? "
            "WHERE id = ?",
            (error, uid),
        )
    finally:
        conn.close()


def pending_utterances(limit: int = 50) -> list[Utterance]:
    """Highest-priority pending utterances, oldest-first within priority."""
    init_db()
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM utterance_queue "
            "WHERE delivered_at IS NULL "
            "ORDER BY priority DESC, id ASC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
        return [Utterance(**dict(r)) for r in rows]
    finally:
        conn.close()
