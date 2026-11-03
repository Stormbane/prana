"""Result model for `narada doctor`: one CheckResult per piece, rolled
into a Report that renders as a human summary, doctor.json and the
phone's status card.

Statuses are three and ordered: green < amber < red. "since" is when
the current state began, as best the evidence shows; `since_approx`
marks a lower bound or an inference (log retention, a directory mtime)
rather than a recorded event.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

GREEN = "green"
AMBER = "amber"
RED = "red"
_RANK = {GREEN: 0, AMBER: 1, RED: 2}

SCHEMA = "narada.doctor/1"
CARD_KIND = "narada.status-card"
CARD_VERSION = 1


def worst(statuses) -> str:
    out = GREEN
    for s in statuses:
        if _RANK[s] > _RANK[out]:
            out = s
    return out


# ── secret hygiene ────────────────────────────────────────────────────
# Defense in depth: checks never read secrets into reasons on purpose,
# but every string that leaves the doctor passes through here anyway.
_REDACTIONS = [
    (re.compile(r"bot\d{6,12}:[A-Za-z0-9_-]{20,}"), "bot<redacted>"),
    (re.compile(r"\b\d{8,12}:AA[A-Za-z0-9_-]{30,}"), "<redacted>"),
    (re.compile(r"(sk-[A-Za-z0-9_-]{4})[A-Za-z0-9_-]{10,}"), r"\1<redacted>"),
    (re.compile(r"([Bb]earer )[A-Za-z0-9._~+/=-]{8,}"), r"\1<redacted>"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),
     "<redacted-jwt>"),
    (re.compile(r"(?i)((?:[a-z0-9]+[_-])*(?:api[_-]?key|key|secret|token|password)[\"' ]*[:=][ \"']*)"
                r"[A-Za-z0-9._/+=-]{12,}"), r"\1<redacted>"),
]


def redact(text: str) -> str:
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


def _redact_obj(obj: Any) -> Any:
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: _redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_redact_obj(v) for v in obj]
    return obj


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class CheckResult:
    id: str
    title: str
    status: str
    reason: str
    since: datetime | None = None
    since_approx: bool = False
    hint: str = ""
    details: dict = field(default_factory=dict)
    duration_ms: int = 0

    def to_dict(self) -> dict:
        return _redact_obj({
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "reason": self.reason,
            "since": iso(self.since),
            "since_approx": self.since_approx,
            "hint": self.hint,
            "details": self.details,
            "duration_ms": self.duration_ms,
        })


@dataclass
class Report:
    generated_at: datetime
    checks: list[CheckResult]
    host: str = ""

    @property
    def overall(self) -> str:
        return worst(c.status for c in self.checks)

    def counts(self) -> dict[str, int]:
        out = {GREEN: 0, AMBER: 0, RED: 0}
        for c in self.checks:
            out[c.status] += 1
        return out

    def ordered(self) -> list[CheckResult]:
        """Worst first; stable within a status (registry order)."""
        return sorted(self.checks, key=lambda c: -_RANK[c.status])

    def exit_code(self) -> int:
        return _RANK[self.overall]

    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA,
            "generated_at": iso(self.generated_at),
            "host": self.host,
            "overall": self.overall,
            "counts": self.counts(),
            "checks": [c.to_dict() for c in self.checks],
            "card": self.to_card(),
        }

    def to_card(self, stale_after_s: int = 3 * 3600) -> dict:
        """The phone Data tab's status card (docs/doctor.md §Status card).
        Self-contained: the app renders it without knowing any check."""
        reds = [c for c in self.checks if c.status == RED]
        ambers = [c for c in self.checks if c.status == AMBER]
        if reds:
            headline = (f"{len(reds)} need{'s' if len(reds) == 1 else ''} "
                        f"attention: " + ", ".join(c.title for c in reds[:3])
                        + ("…" if len(reds) > 3 else ""))
        elif ambers:
            headline = f"Working, {len(ambers)} to watch"
        else:
            headline = "All systems well"
        return _redact_obj({
            "kind": CARD_KIND,
            "version": CARD_VERSION,
            "generated_at": iso(self.generated_at),
            "stale_after_s": stale_after_s,
            "overall": self.overall,
            "headline": headline,
            "counts": self.counts(),
            "items": [
                {
                    "id": c.id,
                    "label": c.title,
                    "status": c.status,
                    "reason": c.reason,
                    "since": iso(c.since),
                    "since_approx": c.since_approx,
                    "action": c.hint or None,
                }
                for c in self.ordered()
            ],
        })
