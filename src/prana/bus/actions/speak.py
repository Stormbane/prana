"""speak — say something. Body if at PC, Telegram if away.

The action-bus wrapper around prana.state.router.route_utterance. Adds:
  - ACTION_INVOKE event published BEFORE the call (durable intent)
  - ACTION_RESULT event published AFTER (durable outcome with
    delivery channel + utterance queue id correlation)
  - Audit trail visible to any subscriber of bus events

Existing direct callers of route_utterance still work — this is an
additive surface, not a replacement. The heartbeat daemon migrates to
use this in unified-mind Phase 1C; future cognitions go through here
from the start.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from prana.bus.events import EventKind, publish
from prana.state.router import RouteResult, route_utterance

logger = logging.getLogger(__name__)


def invoke_speak(
    text: str,
    *,
    source: str,
    topic: str = "",
    priority: int = 0,
    force_channel: Optional[str] = None,
    trace_id: Optional[str] = None,
    budget_hint: Optional[dict[str, Any]] = None,
) -> RouteResult:
    """Speak through the action bus.

    Publishes ACTION_INVOKE, runs the router, publishes ACTION_RESULT,
    returns the RouteResult. Same return shape as direct
    route_utterance — callers that want richer telemetry get it for
    free via the bus events.
    """
    # 1. Publish intent
    invoke_payload = {
        "text": text,
        "topic": topic,
        "priority": priority,
        "force_channel": force_channel,
    }
    invoke_id = publish(
        EventKind.ACTION_INVOKE,
        "speak",
        invoke_payload,
        source=source,
        trace_id=trace_id,
        budget_hint=budget_hint,
    )
    logger.debug("speak invoked #%d source=%s text=%r", invoke_id, source, text[:60])

    # 2. Execute (existing router logic, unchanged)
    result = route_utterance(
        text,
        source=source,
        topic=topic,
        priority=priority,
        force_channel=force_channel,
    )

    # 3. Publish outcome
    result_payload = {
        "invoke_id": invoke_id,
        "utterance_id": result.utterance_id,
        "delivered_to": result.delivered_to,
        "skipped_reason": result.skipped_reason,
        "body_attempted": result.body_attempted,
        "body_error": result.body_error,
        "telegram_attempted": result.telegram_attempted,
        "telegram_error": result.telegram_error,
        "ok": result.ok,
    }
    publish(
        EventKind.ACTION_RESULT,
        "speak",
        result_payload,
        source=source,
        trace_id=trace_id,
    )
    return result
