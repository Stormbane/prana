"""Outbound Telegram from the voice surface — personal tier only (B2).

The round-1 cross-review killed the open-tier version: an
unauthenticated speaker sending under Narada's identity is a direct
outbound mutation, and "a guest can already leave a note" fails because
a note on the fridge doesn't arrive as Narada speaking. So:

- The tool is only REGISTERED for personal-tier sessions (surface), and
  this module re-checks the tier (code) — prompt text never gates it.
- Messages carry origin attribution and pass redaction.
- The rate limiter is DURABLE (state.db): worker restarts cannot reset
  it. Attempts are counted, not successes — a failing send cannot be
  hammered around the cap.
- Delivery failure surfaces to the caller. Never silent.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, Optional

from prana.state.db import get_db
from prana.state.router import route_utterance
from prana.voice.transcripts import redact

logger = logging.getLogger(__name__)

MESSAGES_PER_HOUR = 5
MAX_MESSAGE_CHARS = 800
ORIGIN_PREFIX = "[voice/personal] "

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS voice_outbound_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      REAL NOT NULL,
    session TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_voice_outbound_at ON voice_outbound_log(at);
"""


class NotAllowed(RuntimeError):
    pass


class RateLimited(RuntimeError):
    pass


def send_to_suti(
    text: str,
    *,
    tier: str,
    session_id: str,
    db_path: Optional[Path] = None,
    now: Callable[[], float] = time.time,
    route=route_utterance,
) -> dict:
    """Send a message to Suti's Telegram. Raises NotAllowed /
    RateLimited; returns {'delivered': bool, 'detail': str}."""
    if tier != "personal":
        # Enforced in code regardless of which surface called us.
        raise NotAllowed("outbound messaging requires the personal tier")

    text = redact((text or "").strip())[:MAX_MESSAGE_CHARS]
    if not text:
        raise ValueError("empty message")

    conn = get_db(db_path) if db_path else get_db()
    try:
        conn.executescript(SCHEMA_SQL)
        t = now()
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM voice_outbound_log WHERE at > ?",
            (t - 3600.0,)).fetchone()["n"]
        if n >= MESSAGES_PER_HOUR:
            raise RateLimited(
                f"message limit reached ({MESSAGES_PER_HOUR}/hour)")
        # Count the ATTEMPT before sending: a failing send must consume
        # quota too, or failures become a hammering loophole.
        conn.execute(
            "INSERT INTO voice_outbound_log (at, session) VALUES (?, ?)",
            (t, session_id[:40]))
    finally:
        conn.close()

    result = route(
        ORIGIN_PREFIX + text,
        source="voice-message",
        topic="message-suti",
        force_channel="telegram",
    )
    if result.ok:
        return {"delivered": True, "detail": result.delivered_to}
    detail = result.telegram_error or result.skipped_reason or "unknown"
    logger.warning("message_suti delivery failed: %s", detail)
    return {"delivered": False, "detail": detail}
