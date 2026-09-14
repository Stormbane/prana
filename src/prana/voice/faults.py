"""Voice faults, said plainly (Suti 2026-09-14: "if the credits are out,
a very simple human-readable error message — on the phone app as well
as the box").

A provider failure reaches the workers as a session error: an
APIError with a code, or a connection error that only says "closed".
This module turns that into ONE sentence a person can act on, keeps
the worst one seen in a session, and delivers it everywhere it
matters: the box (a sticky chip on narada.session), the phone (a fault
message on narada.activity), and the brain's notification feed (a
persistent, deduplicated error entry, pushed to the phone).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)

BILLING_URL = "platform.openai.com/settings/organization/billing"

# Worst first: a credit refusal is followed by "connection closed" errors
# that would otherwise win by arriving last.
_RANK = ("credits", "auth", "rate", "model", "connect", "unknown")

TEXT: dict[str, str] = {
    "credits": f"OpenAI credit is empty — no voice until it is topped up at {BILLING_URL}.",
    "auth": "The OpenAI key was rejected — voice is off until the key is fixed.",
    "rate": "OpenAI is rate-limiting voice right now — give it a minute and try again.",
    "model": "The voice model refused to start — try another model in Settings.",
    "connect": "Couldn't reach OpenAI for voice — check the network and try again.",
    "unknown": "Voice failed — see the worker log.",
}

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("credits", re.compile(r"credit_balance_exhausted|no credits remaining|insufficient_quota|exceeded your current quota", re.I)),
    ("auth", re.compile(r"invalid_api_key|incorrect api key|401|unauthorized|authentication", re.I)),
    ("rate", re.compile(r"rate[_ ]limit|429|too many requests|tokens per min", re.I)),
    ("model", re.compile(r"model_not_found|does not exist|unsupported_model|invalid model", re.I)),
    ("connect", re.compile(r"connection closed|connection failed|connect|timed out|timeout|unreachable", re.I)),
)


@dataclass(frozen=True)
class Fault:
    code: str
    text: str

    @property
    def rank(self) -> int:
        return _RANK.index(self.code) if self.code in _RANK else len(_RANK)


def classify(error: object) -> Fault:
    """A fault for anything a session error carries — the error object,
    its cause chain, or plain text."""
    parts: list[str] = []
    seen: set[int] = set()
    cur: object = error
    while cur is not None and id(cur) not in seen and len(parts) < 6:
        seen.add(id(cur))
        parts.append(repr(cur))
        for attr in ("body", "code", "message"):
            v = getattr(cur, attr, None)
            if isinstance(v, (str, dict)):
                parts.append(json.dumps(v) if isinstance(v, dict) else v)
        cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
    text = " ".join(parts)
    for code, pattern in _PATTERNS:
        if pattern.search(text):
            return Fault(code, TEXT[code])
    return Fault("unknown", TEXT["unknown"])


class FaultTracker:
    """The worst fault a session has seen; None until one arrives."""

    def __init__(self) -> None:
        self.current: Fault | None = None

    def note(self, error: object) -> Fault:
        fault = classify(error)
        if self.current is None or fault.rank < self.current.rank:
            self.current = fault
        return self.current

    def clear(self) -> None:
        self.current = None


# ── wire shapes ────────────────────────────────────────────────────────

def box_chip(fault: Fault | None) -> dict:
    """The box's keyed persistent chip (overlay_chip): text sets it,
    empty text clears it. Key "fault" wears the red style in firmware."""
    return {"type": "chip", "key": "fault", "icon": "tool",
            "text": fault.text if fault is not None else ""}


def phone_message(fault: Fault | None) -> dict:
    """The app's sticky fault on narada.activity; code "" clears it."""
    if fault is None:
        return {"type": "fault", "code": "", "text": ""}
    return {"type": "fault", "code": fault.code, "text": fault.text, "sticky": True}


def event_id(fault: Fault, day: _dt.date | None = None) -> str:
    """One feed entry per fault per day — every failed tap must not
    become another notification."""
    return f"voice-fault:{fault.code}:{(day or _dt.date.today()).isoformat()}"


def notify_brain(fault: Fault, surface: str, brain_url: str | None = None,
                 timeout: float = 5.0) -> bool:
    """A level-error entry in the brain's feed (persistent, pushed to
    the phone). Best-effort; never raises."""
    try:
        from prana.brain.tokens import load_brain_tokens

        token = load_brain_tokens().get("prana")
        if not token:
            logger.warning("no prana token: voice fault not posted to the feed")
            return False
        url = (brain_url or os.environ.get("NARADA_BRAIN_URL", "http://127.0.0.1:8811")).rstrip("/")
        body = {
            "level": "error", "title": "Voice", "body": fault.text,
            "source": f"voice-{surface}", "category": "voice",
            "event_id": event_id(fault),
        }
        req = urllib.request.Request(f"{url}/v1/narada/notify", data=json.dumps(body).encode("utf-8"),
                                     headers={"Authorization": f"Bearer {token}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return 200 <= res.status < 300
    except Exception as exc:  # noqa: BLE001 — the fault must still reach the room
        logger.warning("voice fault not posted to the feed: %s", exc)
        return False
