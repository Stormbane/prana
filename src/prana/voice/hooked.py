"""An Agent that awaits a bounded hook before every reply — the fitness
briefing refresh (plan-exercise-corpus §C2; Codex diff review: with
server-side turn detection a reply can start before the transcript
lands, so the refresh must run in the pre-reply hook, not after the
transcript event). A slow or failing hook is skipped: a reply on the
old instructions beats no reply.

Its own module so both the committed workers and the pipeline factory
can build on it.
"""

from __future__ import annotations

import asyncio
import logging

from livekit.agents import Agent

logger = logging.getLogger(__name__)


class HookedAgent(Agent):
    def __init__(self, *args, before_reply=None, hook_timeout_s: float = 2.0, **kwargs):
        super().__init__(*args, **kwargs)
        self._before_reply = before_reply
        self._hook_timeout_s = hook_timeout_s

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        if self._before_reply is not None:
            try:
                await asyncio.wait_for(self._before_reply(), timeout=self._hook_timeout_s)
            except Exception as exc:  # noqa: BLE001 — timeout or failure
                logger.warning("before-reply hook skipped: %s", exc)
        await super().on_user_turn_completed(turn_ctx, new_message)
