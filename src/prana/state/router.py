"""Utterance router — body if at PC, Telegram if away.

Synchronous routing: the heartbeat blocks for ~5s while we try a channel
and either succeed or fall back. This is the "instant" requirement —
Suti said an utterance must reach him without queue-polling latency.

The state.db utterance_queue is still the durable record: every
utterance is push'd before delivery is attempted, and marked with the
channel that succeeded (or the reason it was skipped). This gives us a
full audit trail and a foundation for a future retry drainer.

Channels in priority order:
  1. body (deha /utter at 127.0.0.1:8765) — if proximity says "at PC"
  2. Telegram sendMessage to TELEGRAM_HOME_CHANNEL — fallback
  3. (future) email — when both above fail and the utterance is high-priority

Configuration is read from ~/.hermes/.env (the same file the chat
bridge uses) so we don't fork the secrets store. Falls back gracefully
if Telegram credentials are missing — utterances still reach the body.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from prana.state import utterance_queue
from prana.state.presence import is_present

logger = logging.getLogger(__name__)


# Load Hermes's .env so we share the bot token + chat allowlist
_HERMES_ENV = Path.home() / ".hermes" / ".env"
if _HERMES_ENV.exists():
    load_dotenv(_HERMES_ENV)


DEHA_UTTER_URL = os.environ.get(
    "DEHA_UTTER_URL", "http://127.0.0.1:8765/utter"
)
DEHA_TIMEOUT_S = 5.0

TELEGRAM_TOKEN = os.environ.get("NARADA_TELEGRAM_BOT_TOKEN", "")
TELEGRAM_HOME_CHAT = os.environ.get("TELEGRAM_HOME_CHANNEL", "")
TELEGRAM_TIMEOUT_S = 10.0

PROXIMITY_THRESHOLD_S = float(os.environ.get("NARADA_PROXIMITY_THRESHOLD_S", "120"))


@dataclass
class RouteResult:
    """Outcome of a route_utterance() call."""
    utterance_id: int
    delivered_to: Optional[str]   # 'body' | 'telegram:<chat>' | None if skipped
    skipped_reason: Optional[str]
    body_attempted: bool
    body_error: Optional[str]
    telegram_attempted: bool
    telegram_error: Optional[str]

    @property
    def ok(self) -> bool:
        return self.delivered_to is not None and not self.delivered_to.startswith("skipped:")


def _try_body(text: str, source: str, priority: int) -> tuple[bool, str]:
    """POST to deha /utter. Returns (ok, error_message). Empty error on success."""
    body = json.dumps({
        "text": text,
        "source": source,
        "priority": priority,
    }).encode("utf-8")
    req = urllib.request.Request(
        DEHA_UTTER_URL,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=DEHA_TIMEOUT_S) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        return False, f"deha unreachable: {exc}"

    if resp.get("ok"):
        return True, ""
    return False, f"deha rejected: {resp.get('error', 'unknown')}"


def _try_telegram(text: str) -> tuple[bool, str]:
    """POST to Telegram Bot API sendMessage. Returns (ok, error_message)."""
    if not TELEGRAM_TOKEN:
        return False, "telegram: NARADA_TELEGRAM_BOT_TOKEN not set"
    if not TELEGRAM_HOME_CHAT:
        return False, "telegram: TELEGRAM_HOME_CHANNEL not set"

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    body = urllib.parse.urlencode({
        "chat_id": TELEGRAM_HOME_CHAT,
        "text": text,
    }).encode("utf-8")

    try:
        with urllib.request.urlopen(url, data=body, timeout=TELEGRAM_TIMEOUT_S) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        return False, f"telegram error: {exc}"

    if resp.get("ok"):
        return True, ""
    return False, f"telegram rejected: {resp.get('description', 'unknown')}"


def route_utterance(
    text: str,
    *,
    source: str,
    topic: str = "",
    priority: int = 0,
    force_channel: Optional[str] = None,
) -> RouteResult:
    """Push utterance to queue, then try channels until one succeeds.

    force_channel: if set to 'body' or 'telegram', skip proximity and
    use that channel directly. Useful for testing.
    """
    text = (text or "").strip()
    if not text:
        # Don't queue empty utterances
        return RouteResult(
            utterance_id=-1,
            delivered_to="skipped:empty",
            skipped_reason="empty",
            body_attempted=False,
            body_error=None,
            telegram_attempted=False,
            telegram_error=None,
        )

    uid = utterance_queue.push_utterance(
        text, source=source, topic=topic, priority=priority,
    )

    # Decide order
    if force_channel == "body":
        order = ["body"]
    elif force_channel == "telegram":
        order = ["telegram"]
    elif is_present(pc_idle_threshold_s=PROXIMITY_THRESHOLD_S):
        # Body or PC says Suti is here -> try the body first
        order = ["body", "telegram"]
    else:
        # No signal sees him -> phone first, body as backup
        order = ["telegram", "body"]

    body_attempted = False
    body_error: Optional[str] = None
    telegram_attempted = False
    telegram_error: Optional[str] = None

    for channel in order:
        if channel == "body":
            body_attempted = True
            ok, err = _try_body(text, source, priority)
            if ok:
                utterance_queue.mark_delivered(uid, "body")
                logger.info("utterance #%d -> body: %r", uid, text[:60])
                return RouteResult(
                    utterance_id=uid,
                    delivered_to="body",
                    skipped_reason=None,
                    body_attempted=True,
                    body_error=None,
                    telegram_attempted=telegram_attempted,
                    telegram_error=telegram_error,
                )
            body_error = err
            logger.info("utterance #%d body attempt failed: %s", uid, err)

        elif channel == "telegram":
            telegram_attempted = True
            ok, err = _try_telegram(text)
            if ok:
                channel_label = f"telegram:{TELEGRAM_HOME_CHAT}"
                utterance_queue.mark_delivered(uid, channel_label)
                logger.info("utterance #%d -> %s: %r", uid, channel_label, text[:60])
                return RouteResult(
                    utterance_id=uid,
                    delivered_to=channel_label,
                    skipped_reason=None,
                    body_attempted=body_attempted,
                    body_error=body_error,
                    telegram_attempted=True,
                    telegram_error=None,
                )
            telegram_error = err
            logger.info("utterance #%d telegram attempt failed: %s", uid, err)

    # Both attempted, both failed
    reason = "all-channels-failed"
    detail = "; ".join(
        e for e in [body_error, telegram_error] if e
    )
    utterance_queue.mark_skipped(uid, f"{reason}: {detail}")
    logger.warning("utterance #%d: %s — left pending. %s", uid, reason, detail)
    return RouteResult(
        utterance_id=uid,
        delivered_to=f"skipped:{reason}",
        skipped_reason=detail,
        body_attempted=body_attempted,
        body_error=body_error,
        telegram_attempted=telegram_attempted,
        telegram_error=telegram_error,
    )
