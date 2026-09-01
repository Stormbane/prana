"""Full conversation transcript logging for the voice loop — with the
privacy controls the cross-review (#3) requires before the body goes live.

Every finalized utterance is appended to a per-session markdown file with
UTC timestamps. Controls:
  - owner-only file permissions (Windows ACL locked to the current user)
  - secret redaction before write (API keys, tokens)
  - retention: transcripts older than RETENTION_DAYS are pruned
  - a visible "recording active" marker file the body/indicator can read,
    and explicit RECORDING STARTED / STOPPED lines
  - training-data reuse is opt-in and OUT of this audit tree by default

Fail-open by contract: a transcript/privacy error must NEVER break or drop
the live conversation.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

TRANSCRIPT_ROOT = Path.home() / ".narada" / "heartbeat" / "voice-transcripts"
RECORDING_MARKER = Path.home() / ".narada" / "heartbeat" / ".voice-recording-active"
RETENTION_DAYS = 30

# Secret patterns redacted before any utterance is written to disk.
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),           # OpenAI / Anthropic keys
    re.compile(r"\b[A-Za-z0-9_-]{32,}\.[A-Za-z0-9_-]{16,}\b"),  # tokens/JWT-ish
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),            # AWS
]


def redact(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:40]


def _lock_owner_only(path: Path) -> None:
    """Restrict a file to the current user (Windows icacls). Best-effort."""
    if sys.platform != "win32":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return
    try:
        user = os.environ.get("USERNAME", "")
        # remove inheritance, grant only the owner full control
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r",
                        f"{user}:F"], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("could not lock transcript perms: %s", exc)


def prune_old(root: Path = TRANSCRIPT_ROOT, days: int = RETENTION_DAYS) -> int:
    """Delete transcript files older than `days`. Returns count removed."""
    if not root.is_dir():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    removed = 0
    for f in root.rglob("*.md"):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("pruned %d transcript(s) older than %d days", removed, days)
    return removed


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
    """One markdown file per voice session, owner-locked and redacted."""

    def __init__(self, room_name: str, root: Path = TRANSCRIPT_ROOT) -> None:
        prune_old(root)  # retention on each new session
        now = datetime.now(timezone.utc)
        self.path = (root / now.strftime("%Y_%m") /
                     f"{now.strftime('%Y%m%d-%H%M%S')}-{_safe(room_name)}.md")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                f"# Voice transcript — {room_name}\n\n"
                f"started: {now.isoformat()}\n"
                f"> RECORDING ACTIVE — this conversation is being logged.\n\n",
                encoding="utf-8",
            )
            _lock_owner_only(self.path)
        except OSError as exc:
            logger.warning("transcript init failed (%s): %s", self.path, exc)
        self._set_recording_marker(True, room_name)

    def _set_recording_marker(self, active: bool, room: str = "") -> None:
        """A file the body / a status LED can read to show recording state."""
        try:
            if active:
                RECORDING_MARKER.parent.mkdir(parents=True, exist_ok=True)
                RECORDING_MARKER.write_text(
                    f"recording since {datetime.now(timezone.utc).isoformat()} "
                    f"({room})\n", encoding="utf-8")
            elif RECORDING_MARKER.exists():
                RECORDING_MARKER.unlink()
        except OSError:
            pass

    def log(self, role: str, text: str) -> None:
        text = redact((text or "").strip())
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
                f.write(f"\nended: {ts}{f' ({reason})' if reason else ''}\n"
                        f"> RECORDING STOPPED\n")
        except OSError:
            pass
        self._set_recording_marker(False)


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


def recent_tail(max_age_s: float = 900.0, max_chars: int = 800):
    """The tail of the most recent finished conversation, if it ended
    within `max_age_s` — conversational short-term memory (Suti's
    design, 2026-09-02: a tap five minutes after a cliffhanger should
    remember the cliffhanger; an hour later it may fade).

    Returns (age_seconds, text) or None. Reads only this device's own
    transcripts; redaction was applied at write time.
    """
    import re

    try:
        files = sorted(TRANSCRIPT_ROOT.rglob("*.md"),
                       key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for f in files[:3]:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        m = re.search(r"^ended: (\S+)", text, re.M)
        if not m:
            continue  # unfinished/aborted transcript — skip
        try:
            ended = datetime.fromisoformat(m.group(1))
        except ValueError:
            continue
        age = (datetime.now(timezone.utc) - ended).total_seconds()
        if age > max_age_s:
            return None  # newest finished one is too old — all are
        turns = [ln for ln in text.splitlines() if ln.startswith("- `")]
        if not turns:
            return None
        tail = "\n".join(turns)[-max_chars:]
        return age, tail
    return None
