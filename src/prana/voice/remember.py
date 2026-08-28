"""Voice-originated memory — quarantined by construction (B1).

Everything the voice writes lands in ``~/.narada/inbox/voice/`` and
NOWHERE else. ``inbox`` is on `prana.voice.memory`'s hard denylist
(NEVER_RECALLABLE), so nothing written from the room can ever be
recalled INTO the room — spoken prompt injection cannot install
persistent content (cross-review round 1 #1). Promotion into recallable
branches (notes/projects) is a judgment act on the prana tier: the chat
bridge's review flow and the daily debrief read this directory.

Durability choices: the filesystem is the store AND the quota ledger —
today's write count is today's file count, which survives worker
restarts for free.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from pathlib import Path

from prana.voice.transcripts import redact

logger = logging.getLogger(__name__)

INBOX_DIR = Path.home() / ".narada" / "inbox" / "voice"

MAX_NOTE_CHARS = 600
MAX_SUMMARY_CHARS = 2000
MAX_NOTES_PER_DAY = 40

_DATE_FMT = "%Y-%m-%d"


class QuotaExceeded(RuntimeError):
    pass


def _today() -> str:
    return time.strftime(_DATE_FMT)


def _todays_count(inbox: Path) -> int:
    if not inbox.exists():
        return 0
    return sum(1 for p in inbox.glob(f"{_today()}_*.md"))


def _write(inbox: Path, kind: str, text: str, tier: str,
           session_id: str, max_chars: int) -> Path:
    """The single write path. The filename is OURS (timestamp + random
    suffix) — nothing from the payload reaches the filesystem namespace,
    so path tricks in `text` are inert by construction."""
    text = redact(text.strip())[:max_chars]
    if not text:
        raise ValueError("empty note")
    if _todays_count(inbox) >= MAX_NOTES_PER_DAY:
        raise QuotaExceeded(
            f"voice inbox quota reached ({MAX_NOTES_PER_DAY}/day)")
    inbox.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    name = f"{stamp}_{secrets.token_hex(2)}.md"
    # tier/session go into frontmatter; sanitize to keep the metadata
    # block unbreakable by crafted values.
    safe_tier = re.sub(r"[^a-z-]", "", tier.lower())[:20] or "unknown"
    safe_session = re.sub(r"[^A-Za-z0-9_-]", "", session_id)[:40]
    body = (
        "---\n"
        f"origin: voice\n"
        f"kind: {kind}\n"
        f"tier: {safe_tier}\n"
        f"session: {safe_session}\n"
        f"at: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n"
        "status: pending-review\n"
        "---\n\n"
        f"{text}\n"
    )
    path = inbox / name
    path.write_text(body, encoding="utf-8")
    logger.info("voice inbox: %s (%s, %s)", name, kind, safe_tier)
    return path


def write_note(text: str, tier: str, session_id: str,
               inbox: Path = INBOX_DIR) -> Path:
    """A remember_this note. Both tiers may note; nothing becomes
    recallable without promotion."""
    return _write(inbox, "note", text, tier, session_id, MAX_NOTE_CHARS)


def write_session_summary(text: str, tier: str, session_id: str,
                          inbox: Path = INBOX_DIR) -> Path | None:
    """Session-end summary — personal tier ONLY (a shareable session
    with an unknown speaker does not get to author Narada's memory of
    the day). Returns None when refused."""
    if tier != "personal":
        logger.info("no summary: tier=%s is not personal", tier)
        return None
    return _write(inbox, "session-summary", text, tier, session_id,
                  MAX_SUMMARY_CHARS)
