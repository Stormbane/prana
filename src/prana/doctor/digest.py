"""DESIGN ONLY: the once-daily red digest (docs/doctor.md §Daily digest).

This module composes the text and decides whether a digest is due. It
has no sending code on purpose: nothing here talks to Telegram, the
app or smriti. Enabling it is the documented one-line wiring through
smriti's notify_suti.py, done deliberately by Suti, later.
"""
from __future__ import annotations

import hashlib
from datetime import date

from prana.doctor.model import RED, Report, redact

MAX_CHARS = 3500  # under Telegram's 4096 with room for the fan-out prefix


def compose_digest(report: Report, local_tz) -> str | None:
    """The digest text, or None when nothing is red. One line per red
    check naming what failed (for the nightly: the failing step), when
    it started, and the next action."""
    reds = [c for c in report.ordered() if c.status == RED]
    if not reds:
        return None
    day = report.generated_at.astimezone(local_tz).strftime("%a %d %b")
    lines = [f"Narada doctor, {day}: {len(reds)} red."]
    for c in reds:
        since = ""
        if c.since:
            since = (f" (since {'~' if c.since_approx else ''}"
                     f"{c.since.astimezone(local_tz).strftime('%d %b %H:%M')})")
        lines.append(f"- {c.title}: {c.reason}{since}")
        if c.hint:
            lines.append(f"  next: {c.hint}")
    text = redact("\n".join(lines))
    return text if len(text) <= MAX_CHARS else text[: MAX_CHARS - 1] + "…"


def fingerprint(report: Report) -> str:
    """Stable across runs while the same checks are red for the same
    reasons, so a repeat can be recognised."""
    reds = sorted(f"{c.id}:{c.reason}" for c in report.checks if c.status == RED)
    return hashlib.sha256("\n".join(reds).encode("utf-8")).hexdigest()[:16]


def digest_due(report: Report, state: dict | None, today: date) -> bool:
    """At most one digest per local day, and only when something is red.
    `state` is the last-sent record {"day": "YYYY-MM-DD", "fingerprint": …}."""
    if not any(c.status == RED for c in report.checks):
        return False
    return not state or state.get("day") != today.isoformat()
