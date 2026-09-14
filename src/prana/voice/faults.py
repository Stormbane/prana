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

import collections
import datetime as _dt
import json
import logging
import os
import pathlib
import re
import time as _time
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


# The provider's own words. The LiveKit OpenAI plugin logs the real
# error ({"code": "credit_balance_exhausted", …}) as a log record extra
# ("lk.pii.error") and then raises a generic APIError("GPT-Live returned
# an error") with no body — so the exception alone classifies as a
# connection fault (field, 2026-09-14). A handler on the plugin's logger
# keeps the last few provider errors; note() consults them.
RECENT_S = 30.0
_recent: collections.deque[tuple[float, str]] = collections.deque(maxlen=8)


class _ProviderErrorTap(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        detail = record.__dict__.get("lk.pii.error")
        if isinstance(detail, dict):
            try:
                _recent.append((_time.monotonic(), json.dumps(detail)))
            except (TypeError, ValueError):
                _recent.append((_time.monotonic(), str(detail)))
        elif record.levelno >= logging.ERROR and "error" in record.getMessage().lower():
            _recent.append((_time.monotonic(), record.getMessage()))


_tap_installed = False


def install_provider_tap() -> None:
    """Once per process: listen to the plugin loggers."""
    global _tap_installed
    if _tap_installed:
        return
    _tap_installed = True
    for name in ("livekit.plugins.openai", "livekit.agents"):
        logging.getLogger(name).addHandler(_ProviderErrorTap())


def recent_provider_errors(now: float | None = None) -> list[str]:
    now = _time.monotonic() if now is None else now
    return [text for at, text in _recent if now - at <= RECENT_S]


def _forget_recent() -> None:
    _recent.clear()


class FaultTracker:
    """The worst fault a session has seen; None until one arrives."""

    def __init__(self) -> None:
        self.current: Fault | None = None
        install_provider_tap()

    def note(self, error: object) -> Fault:
        fault = classify(error)
        # The provider's words outrank a generic exception.
        for text in recent_provider_errors():
            other = classify(text)
            if other.rank < fault.rank:
                fault = other
        if self.current is None or fault.rank < self.current.rank:
            self.current = fault
        return self.current

    def clear(self) -> None:
        self.current = None


# ── the fault outlives the job ─────────────────────────────────────────
# The box worker retires its job after every session (the room is
# deleted, the box rejoins, a fresh job is dispatched) and the box
# wipes its chips on disconnect. A chip published once is therefore
# gone within seconds unless every fresh job re-asserts it — as the TV
# chip is. The last unspoken fault lives in a small file until a later
# session speaks.

def _fault_file() -> pathlib.Path:
    base = os.environ.get("NARADA_STATE_DIR") or os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "narada")
    return pathlib.Path(base) / "voice-fault.json"


def remember(fault: Fault) -> None:
    try:
        f = _fault_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"code": fault.code, "text": fault.text,
                                 "at": _dt.datetime.now().isoformat(timespec="seconds")}),
                     encoding="utf-8")
    except OSError as exc:
        logger.warning("voice fault not remembered: %s", exc)


def forget() -> None:
    try:
        _fault_file().unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("voice fault file not removed: %s", exc)


def remembered() -> Fault | None:
    try:
        raw = json.loads(_fault_file().read_text(encoding="utf-8"))
        code, text = raw.get("code"), raw.get("text")
        if isinstance(code, str) and code in TEXT and isinstance(text, str) and text:
            return Fault(code, text)
    except (OSError, ValueError):
        pass
    return None


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
