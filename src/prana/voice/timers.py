"""Timers, alarms, reminders — durable, tiered, idempotent (C1).

Cross-review #6 shaped the tier rules: a shareable-tier caller must not
be able to schedule delayed Telegram delivery (that would bypass B2's
cap at a distance). So:

- shareable-tier entries are LOCAL-ONLY: they announce on the body
  (chime + presentation hint once B5's audio owner lands; until then
  the firing is recorded and shown as a face hint if a session is
  open — never silently dropped: undeliverable local firings fall back
  to the personal ledger only if the creator was personal).
- personal-tier reminders may deliver to Telegram, and B2's durable
  rate limit is enforced AT DELIVERY TIME too (messaging.send_to_suti
  is the only door).

Durability: state.db. Firing is idempotent — a row is claimed with a
transactional UPDATE before delivery, so a worker restart can neither
double-fire nor lose a due entry (unclaimed rows re-fire on the next
sweep).

Bounds: pending count, horizon, text length. Cancellation of a
personal-tier entry requires the personal tier.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, Optional

from prana.state.db import get_db
from prana.voice.transcripts import redact

logger = logging.getLogger(__name__)

MAX_PENDING = 20
MAX_HORIZON_S = 14 * 24 * 3600.0
MAX_TEXT_CHARS = 200
MIN_DURATION_S = 5.0

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS voice_timers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   REAL NOT NULL,
    fire_at      REAL NOT NULL,
    text         TEXT NOT NULL,
    kind         TEXT NOT NULL,            -- 'timer' | 'reminder'
    origin_tier  TEXT NOT NULL,
    session      TEXT NOT NULL DEFAULT '',
    fired_at     REAL,
    delivered    TEXT,                     -- how it reached someone
    cancelled_at REAL
);
CREATE INDEX IF NOT EXISTS idx_voice_timers_due
    ON voice_timers(fire_at) WHERE fired_at IS NULL AND cancelled_at IS NULL;
"""


class TimerError(RuntimeError):
    pass


def _conn(db_path: Optional[Path]):
    c = get_db(db_path) if db_path else get_db()
    c.executescript(SCHEMA_SQL)
    return c


def create(
    text: str,
    fire_in_s: float,
    *,
    kind: str,
    tier: str,
    session_id: str,
    db_path: Optional[Path] = None,
    now: Callable[[], float] = time.time,
) -> dict:
    """Schedule. Returns {'id', 'fire_at'}. Raises TimerError on bounds."""
    text = redact((text or "").strip())[:MAX_TEXT_CHARS]
    if not text:
        raise TimerError("empty text")
    if kind not in ("timer", "reminder"):
        raise TimerError(f"bad kind {kind!r}")
    if fire_in_s < MIN_DURATION_S:
        raise TimerError(f"too soon (min {MIN_DURATION_S:.0f}s)")
    if fire_in_s > MAX_HORIZON_S:
        raise TimerError("horizon is 14 days")
    c = _conn(db_path)
    try:
        pending = c.execute(
            "SELECT COUNT(*) AS n FROM voice_timers "
            "WHERE fired_at IS NULL AND cancelled_at IS NULL").fetchone()["n"]
        if pending >= MAX_PENDING:
            raise TimerError(f"too many pending ({MAX_PENDING} max)")
        t = now()
        cur = c.execute(
            "INSERT INTO voice_timers "
            "(created_at, fire_at, text, kind, origin_tier, session) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (t, t + fire_in_s, text, kind,
             "personal" if tier == "personal" else "shareable",
             session_id[:40]))
        return {"id": cur.lastrowid, "fire_at": t + fire_in_s}
    finally:
        c.close()


def cancel(timer_id: int, *, tier: str,
           db_path: Optional[Path] = None,
           now: Callable[[], float] = time.time) -> bool:
    """Cancel a pending entry. Personal-tier entries need the personal
    tier; shareable entries may be cancelled by anyone present."""
    c = _conn(db_path)
    try:
        row = c.execute(
            "SELECT origin_tier FROM voice_timers WHERE id = ? "
            "AND fired_at IS NULL AND cancelled_at IS NULL",
            (timer_id,)).fetchone()
        if row is None:
            return False
        if row["origin_tier"] == "personal" and tier != "personal":
            raise TimerError("that reminder needs the personal tier to cancel")
        c.execute("UPDATE voice_timers SET cancelled_at = ? WHERE id = ?",
                  (now(), timer_id))
        return True
    finally:
        c.close()


def list_pending(db_path: Optional[Path] = None) -> list[dict]:
    c = _conn(db_path)
    try:
        rows = c.execute(
            "SELECT id, fire_at, text, kind, origin_tier FROM voice_timers "
            "WHERE fired_at IS NULL AND cancelled_at IS NULL "
            "ORDER BY fire_at ASC LIMIT 50").fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


def sweep_due(
    *,
    db_path: Optional[Path] = None,
    now: Callable[[], float] = time.time,
    announce_local: Optional[Callable[[dict], bool]] = None,
    send_personal=None,
) -> list[dict]:
    """Fire everything due. Idempotent: each row is CLAIMED (fired_at
    set) in a guarded UPDATE before delivery is attempted; a crash
    after claim loses at most one announcement (recorded as such), and
    a concurrent/restarted sweeper can never double-fire.

    announce_local(entry) -> bool: body-side announcement (chime/hint).
    send_personal(text) -> dict: Telegram path (B2's capped door).
    Returns the entries fired this sweep.
    """
    c = _conn(db_path)
    fired = []
    try:
        t = now()
        due = c.execute(
            "SELECT * FROM voice_timers WHERE fire_at <= ? "
            "AND fired_at IS NULL AND cancelled_at IS NULL "
            "ORDER BY fire_at ASC LIMIT 10", (t,)).fetchall()
        for row in due:
            claimed = c.execute(
                "UPDATE voice_timers SET fired_at = ? "
                "WHERE id = ? AND fired_at IS NULL", (t, row["id"]))
            if claimed.rowcount != 1:
                continue  # someone else fired it
            entry = dict(row)
            delivered = "none"
            locally = bool(announce_local and announce_local(entry))
            if locally:
                delivered = "body"
            if entry["origin_tier"] == "personal" and not locally:
                # Fall through to Telegram — via B2's capped, redacted
                # door, so delayed delivery cannot bypass the cap.
                if send_personal is not None:
                    try:
                        r = send_personal(
                            f"⏰ {entry['kind']}: {entry['text']}")
                        delivered = ("telegram" if r.get("delivered")
                                     else f"telegram-failed:{r.get('detail')}")
                    except Exception as exc:
                        delivered = f"telegram-failed:{type(exc).__name__}"
            c.execute("UPDATE voice_timers SET delivered = ? WHERE id = ?",
                      (delivered, row["id"]))
            entry["delivered"] = delivered
            fired.append(entry)
            logger.info("timer %d fired -> %s", row["id"], delivered)
        return fired
    finally:
        c.close()
