"""Questling v1 on the brain: today's cards and their tap answers
(akhada spec-questling-v1 §3, §6).

A TAP is a direct, deterministic commit — no model turn — so it works
from any surface (a chat, the Notifications feed, a lock-screen action).
It must carry the client's `event_id`: a retried tap is the same write,
and the same id with a different payload is refused by akhada's
idempotent door. Typed or spoken answers never come here; they go
through the chat agent, which calls the same akhada tools.

Every answer returns the fresh `today` so the app replaces each card by
`card_id` when its `rev` is higher.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import Depends, HTTPException, Request

logger = logging.getLogger(__name__)

_KINDS = ("check", "number", "choice", "skip", "stop")


def _store():
    try:
        from akhada.store.db import Store
    except ImportError:
        raise HTTPException(503, "akhada not installed on this host")
    return Store()


def _answer(body: dict[str, Any]) -> dict[str, Any]:
    from akhada import asks
    from akhada.tools.items import ToolError

    store = _store()
    kind = body.get("kind")
    identity = str(body.get("identity") or "")
    event_id = str(body.get("event_id") or "")
    payload = body.get("payload") or {}
    if kind not in _KINDS:
        raise HTTPException(400, f"kind must be one of {_KINDS}")
    if not event_id or len(event_id) > 120:
        raise HTTPException(400, "a tap needs an event_id (1–120 chars) so a retry is one write")
    if not isinstance(payload, dict):
        raise HTTPException(400, "payload must be an object")
    common = {"event_id": event_id, "session_id": "app-tap", "source": "app"}
    # The day page the tap came from (redesign L2a): the evening check is
    # answered for that day (yesterday's until noon, akhada decides); every
    # other ask carries over to today, so a tap aimed at an older page is
    # refused rather than written to today by surprise.
    day = body.get("day")
    if day is not None and not isinstance(day, str):
        raise HTTPException(400, "day must be a date string (YYYY-MM-DD)")
    # Only a tap that carries its own time can be judged here (the app's
    # always do): without one, a retry of an answer that already committed
    # must reach akhada's replay rather than be refused (review).
    if day is not None and kind != "check" and payload.get("at"):
        from akhada import clock
        today = asks.reporting_day(clock.now())
        # The day the tap was made counts too: an answer queued offline at
        # 23:58 and delivered after 04:00 still reports on its own day.
        try:
            made = asks.reporting_day(clock.parse_at(payload.get("at")))
        except ValueError as exc:  # bad client input is a 400, not a 502 (review)
            raise HTTPException(400, f"payload.at is not a time: {exc}") from None
        if day not in (today, made):
            msg = "this ask carried over to today; answer it on today's page"
            return {"result": {"ok": False, "closed": True, "day": day, "error": msg, "spoken": msg},
                    "today": asks.get_today(store)}
    try:
        if kind == "check":
            if identity != "check":
                raise HTTPException(400, "kind check answers identity check")
            result = asks.log_day_check(
                store, food=payload.get("food"), trained=payload.get("trained"),
                mood=payload.get("mood"), note=payload.get("note"),
                at=payload.get("at"), expected_revision=body.get("expected_revision"),
                via="tap", **({"day": day} if day is not None else {}), **common)
        elif kind == "number":
            value = payload.get("value")
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise HTTPException(400, "payload.value must be a number")
            result = asks.answer_number(store, identity=identity, value=float(value),
                                        at=payload.get("at"), **common)
        elif kind == "choice":
            from akhada import followups
            choice, text = payload.get("choice"), payload.get("text")
            if identity.startswith("followup:"):
                try:
                    fid = int(identity.split(":", 1)[1])
                except ValueError:
                    raise HTTPException(400, "followup identity is followup:<id>")
                result = followups.answer_followup(store, followup_id=fid, choice=choice,
                                                   text=text, at=payload.get("at"),
                                                   via="tap", **common)
            else:
                result = followups.answer_choice(store, identity=identity, choice=choice,
                                                 text=text, at=payload.get("at"),
                                                 via="tap", **common)
        else:
            # The tap's frozen occurrence time decides the day it excuses,
            # never its arrival time (app review P1).
            from akhada import clock
            day = asks.reporting_day(clock.parse_at(payload.get("at")))
            result = asks.excuse_request(store, identity=identity, how=kind, day=day, **common)
    except ToolError as exc:
        result = {"ok": False, "error": str(exc), "spoken": str(exc)}
    except ValueError as exc:
        result = {"ok": False, "error": str(exc), "spoken": str(exc)}
    return {"result": result, "today": asks.get_today(store)}


def register_today(app, require_tier) -> None:
    @app.get("/api/today")
    async def today(tier: str = Depends(require_tier)) -> dict:
        """Today's request cards, full-report progress, mana, LDR."""
        def _read() -> dict:
            from akhada import asks
            return asks.get_today(_store())
        try:
            return await asyncio.to_thread(_read)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("today failed: %s", exc)
            raise HTTPException(502, f"today failed: {exc}")

    @app.post("/api/today/answer")
    async def today_answer(request: Request, tier: str = Depends(require_tier)) -> dict:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "body must be JSON")
        if not isinstance(body, dict):
            raise HTTPException(400, "body must be an object")
        try:
            return await asyncio.to_thread(_answer, body)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("today answer failed: %s", exc)
            raise HTTPException(502, f"answer failed: {exc}")
