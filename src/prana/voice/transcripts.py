"""Full conversation transcript logging for the voice loop.

Every finalized utterance — Suti's speech (transcribed) and Narada's
replies — is appended to a per-session markdown file with UTC
timestamps. This is the audit trail of what the body heard and said,
and future training material for the wake word and persona.

Fail-open by contract: a transcript write must NEVER break or drop the
live conversation. Every path swallows OSError and logs a warning.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

TRANSCRIPT_ROOT = Path.home() / ".narada" / "heartbeat" / "voice-transcripts"


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:40]


def text_of(item) -> str:
    """Extract text from a livekit ChatMessage, defensively."""
    t = getattr(item, "text_content", None)
    if isinstance(t, str) and t:
        return t
    c = getattr(item, "content", None)
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(x for x in c if isinstance(x, str))
    return ""


class TranscriptLogger:
    """One markdown file per voice session."""

    def __init__(self, room_name: str, root: Path = TRANSCRIPT_ROOT) -> None:
        now = datetime.now(timezone.utc)
        self.path = (root / now.strftime("%Y_%m") /
                     f"{now.strftime('%Y%m%d-%H%M%S')}-{_safe(room_name)}.md")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                f"# Voice transcript — {room_name}\n\n"
                f"started: {now.isoformat()}\n\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("transcript init failed (%s): %s", self.path, exc)

    def log(self, role: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        line = f"- `{ts}` **{role}:** {text}\n"
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError as exc:
            logger.warning("transcript write failed (%s): %s", self.path, exc)

    def close(self, reason: str = "") -> None:
        ts = datetime.now(timezone.utc).isoformat()
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(f"\nended: {ts}{f' ({reason})' if reason else ''}\n")
        except OSError:
            pass


def attach(session, room_name: str) -> TranscriptLogger:
    """Wire a TranscriptLogger onto an AgentSession's conversation events.

    Returns the logger (call .close() when the session ends).
    """
    tl = TranscriptLogger(room_name)

    @session.on("conversation_item_added")
    def _on_item(ev) -> None:  # sync handler, fail-open
        try:
            item = getattr(ev, "item", None)
            role = getattr(item, "role", "?")
            tl.log(str(role), text_of(item))
        except Exception as exc:  # never take the session down
            logger.warning("transcript hook failed: %s", exc)

    return tl
