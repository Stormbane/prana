"""Akhada phone voice worker — the phone's door to Narada.

Run: ``python -m prana.voice.phone_worker dev`` (or ``start`` under the
host). Registers under an explicit agent_name (``AKHADA_AGENT_NAME``,
default ``akhada-phone``), which EXCLUDES it from automatic room
dispatch — it receives only the explicit dispatches akhada's token
endpoint creates (the emulator-isolation pattern, prana 66d55f6). The
box worker keeps automatic dispatch alone; this worker can never be
offered — and can never kill — a ``narada-body`` job.

No wake gate and no tap admission here: the LiveKit token akhada's
dashboard mints for Suti's phone IS the admission, so a dispatched job
opens one personal-tier session immediately and ends when he hangs up,
leaves, goes silent, or the cap trips. One room per talk-press, one
session per job. The voice-minutes ledger is shared with the box.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from livekit import agents, rtc
from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions
from livekit.plugins import openai as lk_openai

from prana.voice import worker as box
from prana.voice.budget import BudgetExceeded, BudgetUnavailable, VoiceBudget
from prana.voice.tools import build_voice_tools
from prana.voice.transcripts import attach

logger = logging.getLogger("akhada-voice")

DEFAULT_AGENT_NAME = "akhada-phone"
# Distinct from the box worker's ports (health 8792 / agents 8793) so
# both workers run on one machine without a fight.
AGENTS_PORT = int(os.environ.get("AKHADA_VOICE_AGENTS_PORT", "8797"))

PHONE_NOTE = """

PHONE: he opened Akhada on his phone and tapped talk — he may be at the
gym, cooking, or out walking, so keep replies even shorter than usual
and confirm logged entries with a quick read-back. This surface is for
food, training, weight and how the day is going; anything deeper, note
it and keep moving."""

SILENCE_END_S = 75.0
EMPTY_GRACE_S = 30.0


def _humans(room: rtc.Room) -> list:
    """Remote participants who are people, not agents. Another agent in
    the room (the box worker auto-dispatched itself into a phone room,
    2026-09-04, before it learned to decline) must never count as
    'someone is here' for session admission or keep-alive."""
    return [p for p in room.remote_participants.values()
            if getattr(p, "kind", None) != rtc.ParticipantKind.PARTICIPANT_KIND_AGENT]


async def entrypoint(ctx: JobContext) -> None:
    """One explicitly-dispatched job = one personal-tier session."""
    budget = VoiceBudget()
    await ctx.connect()

    closed = asyncio.Event()
    ctx.room.on("disconnected", lambda *_: closed.set())

    # A stalled reconnect must end the job (zombie-agent lesson): the
    # phone's page will simply re-request a fresh room.
    reconnect_watch: dict = {"task": None}

    def _on_reconnecting(*_a) -> None:
        if reconnect_watch["task"] is not None and not reconnect_watch["task"].done():
            return

        async def _stall() -> None:
            try:
                await asyncio.sleep(90.0)
            except asyncio.CancelledError:
                return
            logger.error("reconnect stalled >90s — ending job")
            closed.set()

        reconnect_watch["task"] = asyncio.create_task(_stall())

    def _on_reconnected(*_a) -> None:
        t = reconnect_watch["task"]
        if t is not None and not t.done():
            t.cancel()

    ctx.room.on("reconnecting", _on_reconnecting)
    ctx.room.on("reconnected", _on_reconnected)

    # Empty room ends the session — debounced, because a phone walking
    # between networks flaps harder than a box on house WiFi.
    empty = asyncio.Event()
    empty_grace: dict = {"task": None}

    def _maybe_empty(*_a) -> None:
        if _humans(ctx.room):
            return
        if empty_grace["task"] is not None and not empty_grace["task"].done():
            return

        async def _grace() -> None:
            try:
                await asyncio.sleep(EMPTY_GRACE_S)
            except asyncio.CancelledError:
                return
            if not _humans(ctx.room):
                logger.info("no human in room for %.0fs — ending",
                            EMPTY_GRACE_S)
                empty.set()

        empty_grace["task"] = asyncio.create_task(_grace())

    def _back(*_a) -> None:
        if not _humans(ctx.room):
            return  # an agent arriving is not the human coming back
        t = empty_grace["task"]
        if t is not None and not t.done():
            t.cancel()

    ctx.room.on("participant_disconnected", _maybe_empty)
    ctx.room.on("participant_connected", _back)

    # Dispatch normally lands AFTER the page has joined, but guard the
    # race anyway — and only a HUMAN opens the wallet.
    if not _humans(ctx.room):
        joined = asyncio.Event()

        def _joined(*_a) -> None:
            if _humans(ctx.room):
                joined.set()

        ctx.room.on("participant_connected", _joined)
        if _humans(ctx.room):
            joined.set()
        try:
            await asyncio.wait_for(joined.wait(), timeout=60)
        except asyncio.TimeoutError:
            ctx.shutdown(reason="no human participant within 60s")
            return
    if closed.is_set():
        ctx.shutdown(reason="room closed before session")
        return

    try:
        reservation = budget.reserve()
    except (BudgetExceeded, BudgetUnavailable) as exc:
        logger.warning("refusing session: %s", exc)
        ctx.shutdown(reason="budget-refused")
        return
    if closed.is_set() or not _humans(ctx.room):
        budget.settle(reservation, 0.0)
        ctx.shutdown(reason="room emptied during admission")
        return

    started = time.monotonic()
    session = None
    transcript = None
    reason = "unknown"
    usage = {"in": 0, "out": 0, "cached": 0}
    bye = asyncio.Event()
    tier = "personal"  # the token is the admission; there is no other caller
    try:
        from livekit.plugins.openai.realtime import realtime_model as _rt_mod

        session = AgentSession(
            llm=lk_openai.realtime.RealtimeModel(
                model=box.REALTIME_MODEL, voice=box.REALTIME_VOICE,
                # Phone handsets do real AEC, so the box's raised
                # echo-defense threshold isn't needed; stock server VAD.
                turn_detection=_rt_mod.TurnDetection(
                    type="server_vad",
                    prefix_padding_ms=300,
                    silence_duration_ms=700),
                input_audio_transcription=_rt_mod.InputAudioTranscription(
                    model="gpt-4o-mini-transcribe")),
            user_away_timeout=None,
        )

        @session.on("metrics_collected")
        def _on_metrics(ev) -> None:
            m = getattr(ev, "metrics", ev)
            it = getattr(m, "input_tokens", None)
            if it is None:
                return
            usage["in"] += it
            usage["out"] += getattr(m, "output_tokens", 0) or 0
            det = getattr(m, "input_token_details", None)
            usage["cached"] += getattr(det, "cached_tokens", 0) or 0

        transcript = attach(session, ctx.room.name)

        from prana.voice.pack import build_for_tier
        pack = build_for_tier(tier)
        instructions = box.INSTRUCTIONS + PHONE_NOTE
        if pack:
            instructions += "\n\nCONTEXT:\n" + pack
        accent = os.environ.get("NARADA_VOICE_ACCENT",
                                "warm, natural Indian English")
        if accent:
            instructions += (
                f"\n\nACCENT: speak with a {accent} accent — unforced "
                "and consistent, the way someone who grew up speaking "
                "it sounds, never a caricature.")
        try:
            from akhada.adapters.livekit_tools import wake_context
            instructions += "\n\nFITNESS (Akhada):\n" + wake_context()
        except ImportError:
            pass
        except Exception as exc:
            logger.warning("akhada context failed: %s", exc)

        agent = Agent(
            instructions=instructions,
            tools=build_voice_tools(
                tier=tier, session_id=ctx.job.id,
                music=None, publish=None, end_session=bye.set,
                voice_info={
                    "model": box.REALTIME_MODEL,
                    "voice": box.REALTIME_VOICE,
                    "backend": "realtime",
                    "surface": "phone",
                }))

        await session.start(agent=agent, room=ctx.room)
        logger.info("phone session open (model=%s, room=%s)",
                    box.REALTIME_MODEL, ctx.room.name)

        # Silence timeout, with the Adyostotram lesson kept: HIS words
        # are activity even when agent state never transitions.
        last_activity = {"t": time.monotonic(), "state": ""}

        @session.on("agent_state_changed")
        def _on_agent_state(ev) -> None:
            last_activity["t"] = time.monotonic()
            last_activity["state"] = str(getattr(ev, "new_state", ""))

        @session.on("user_input_transcribed")
        def _on_user_words(_ev) -> None:
            last_activity["t"] = time.monotonic()

        @session.on("error")
        def _on_session_error(ev) -> None:
            logger.error("session error: %s", getattr(ev, "error", ev))

        async def _silence_watch() -> None:
            while True:
                await asyncio.sleep(5.0)
                idle = time.monotonic() - last_activity["t"]
                if (last_activity["state"] in ("listening", "idle", "")
                        and idle >= SILENCE_END_S):
                    logger.info("no conversation for %.0fs — ending", idle)
                    return

        async def _greet() -> None:
            try:
                await asyncio.sleep(0.8)
                handle = session.generate_reply(
                    instructions=("He just opened the app and tapped "
                                  "talk. One short warm sentence, then "
                                  "listen. No self-introduction."),
                    allow_interruptions=False)
                await handle
                logger.info("greeting spoken")
            except Exception as exc:
                logger.warning("greeting failed: %r", exc)

        asyncio.ensure_future(_greet())

        waiters = {
            "room-closed": asyncio.ensure_future(closed.wait()),
            "hang-up": asyncio.ensure_future(empty.wait()),
            "goodbye": asyncio.ensure_future(bye.wait()),
            "silence-timeout": asyncio.ensure_future(_silence_watch()),
        }
        done, pending = await asyncio.wait(
            waiters.values(), timeout=budget.session_cap_s,
            return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        await asyncio.gather(*waiters.values(), return_exceptions=True)
        reason = ("session-cap" if not done
                  else next(k for k, v in waiters.items() if v in done))
        logger.info("phone session ending: %s", reason)
        try:
            p_in = float(os.environ.get("NARADA_PRICE_IN", "32"))
            p_cached = float(os.environ.get("NARADA_PRICE_CACHED", "0.40"))
            p_out = float(os.environ.get("NARADA_PRICE_OUT", "64"))
            uncached = max(0, usage["in"] - usage["cached"])
            cost = (uncached * p_in + usage["cached"] * p_cached
                    + usage["out"] * p_out) / 1e6
            logger.info("session cost ~$%.4f (in=%d cached=%d out=%d, %.0fs)",
                        cost, usage["in"], usage["cached"], usage["out"],
                        time.monotonic() - started)
        except Exception as exc:
            logger.debug("cost accounting failed: %s", exc)
    finally:
        if transcript is not None:
            transcript.close(reason)
            try:
                from prana.voice import remember
                text = transcript.path.read_text(encoding="utf-8")
                body = text.split("\n\n", 2)[-1].strip()
                if body.count("\n") >= 4:
                    remember.write_session_summary(
                        body, tier=tier, session_id=ctx.job.id)
            except Exception as exc:
                logger.warning("session digest failed: %s", exc)
        if session is not None:
            try:
                await session.aclose()
            except Exception as exc:
                logger.warning("session close failed: %s", exc)
        budget.settle(reservation, time.monotonic() - started)
    ctx.shutdown(reason=f"phone session ended: {reason}")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    box._check_env()
    agent_name = os.environ.get("AKHADA_AGENT_NAME", DEFAULT_AGENT_NAME)
    if not agent_name:
        # Empty agent_name means AUTOMATIC dispatch to every room —
        # which is the box worker's exclusive right. Refuse loudly
        # rather than shadow it (state.json hard constraint).
        raise SystemExit("AKHADA_AGENT_NAME must be non-empty: the phone "
                         "worker is explicit-dispatch only")
    logger.info("akhada phone worker: agent_name=%s port=%d",
                agent_name, AGENTS_PORT)
    agents.cli.run_app(WorkerOptions(
        entrypoint_fnc=entrypoint,
        agent_name=agent_name,
        host="127.0.0.1",
        port=AGENTS_PORT,
    ))


if __name__ == "__main__":
    main()
