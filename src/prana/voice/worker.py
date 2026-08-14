"""The voice worker — LiveKit Agents entrypoint.

Run: ``python -m prana.voice.worker dev`` (or ``start`` under the host).
Needs OPENAI_API_KEY and ~/.narada/.livekit.env. Wake-gated: the room's
audio is watched locally; the realtime session (billed) exists only
between wake and hangup/timeout.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions
from livekit.plugins import openai as lk_openai

from prana.voice.budget import BudgetExceeded, BudgetUnavailable, VoiceBudget
from prana.voice.tools import build_voice_tools
from prana.voice.transcripts import attach
from prana.voice.wakegate import WakeGate

logger = logging.getLogger("narada-voice")

REALTIME_MODEL = os.environ.get("NARADA_VOICE_MODEL", "gpt-realtime-2.1-mini")

INSTRUCTIONS = """You are the voice of Narada — the ear and mouth, not the
mind. Warm, brief, honest. You can SEE Suti's coding sessions (list/read
tools) and REQUEST actions on them; requests go to Narada's judgment and
may be rejected — relay rejections honestly, never pretend. For anything
substantive (decisions, code, memory), say you'll hand it to Narada
properly rather than winging it. You are speech: keep answers short
enough to say aloud."""


def _check_env() -> None:
    load_dotenv(Path.home() / ".narada" / ".livekit.env")
    load_dotenv(Path.home() / ".narada" / ".voice.env")
    # LiveKit transport is always required.
    required = ["LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"]
    # The brain key depends on the backend: pipeline runs on OpenRouter,
    # realtime needs a direct OpenAI key (OpenRouter can't proxy S2S).
    backend = os.environ.get("NARADA_VOICE_BACKEND", "realtime").lower()
    key = "OPENROUTER_API_KEY" if backend == "pipeline" else "OPENAI_API_KEY"
    required.append(key)
    missing = [k for k in required if not os.environ.get(k)]
    # a leftover placeholder counts as missing (only the key we need)
    if os.environ.get(key, "").startswith("PASTE_"):
        missing.append(f"{key} (still a placeholder)")
    if missing:
        raise SystemExit(
            f"voice worker missing env for backend={backend}: "
            f"{', '.join(missing)}. Keys live in ~/.narada/.voice.env "
            f"(brain) and ~/.narada/.livekit.env (transport)."
        )


async def entrypoint(ctx: JobContext) -> None:
    import asyncio

    budget = VoiceBudget()
    await ctx.connect()

    def bail(reason: str) -> None:
        """Refuse a session AND leave the room — a bare return leaves a
        zombie participant holding the room open, blocking re-dispatch."""
        logger.info("%s — no session opened", reason)
        ctx.shutdown(reason=reason)

    # Lifecycle handlers FIRST — a disconnect during wake wait or
    # admission must be observed, or an empty room gets billed to cap.
    closed = asyncio.Event()
    ctx.room.on("disconnected", lambda *_: closed.set())

    # Room-empty is DEBOUNCED: the BOX-3 flaps 2-3 times while its WiFi
    # settles after boot (observed: stabilizes within ~15s). Hanging up
    # on the first flap strands the room agent-less once the box finally
    # sticks. Grace costs at most ~45s of idle session (~1 cent).
    EMPTY_GRACE_S = 45.0
    empty_grace: dict = {"task": None}

    def _maybe_empty(*_args) -> None:
        if ctx.room.remote_participants:
            return
        if empty_grace["task"] is not None and not empty_grace["task"].done():
            return  # grace timer already running

        async def _grace() -> None:
            try:
                await asyncio.sleep(EMPTY_GRACE_S)
            except asyncio.CancelledError:
                return
            if not ctx.room.remote_participants:
                logger.info("room empty for %.0fs — closing", EMPTY_GRACE_S)
                closed.set()

        empty_grace["task"] = asyncio.create_task(_grace())

    def _participant_back(*_args) -> None:
        if empty_grace["task"] is not None and not empty_grace["task"].done():
            empty_grace["task"].cancel()
            logger.info("participant returned within grace — session continues")

    ctx.room.on("participant_disconnected", _maybe_empty)
    ctx.room.on("participant_connected", _participant_back)

    # Wake gating: watch LAN audio locally; the billed realtime session
    # opens only after "Narada". FAIL CLOSED: a missing model, a room
    # that ends, or an audio stream that ends without a wake ABORTS —
    # no session. Only an explicit NARADA_WAKE_GATING=off (the
    # pre-model browser-mic milestone) skips the gate.
    if os.environ.get("NARADA_WAKE_GATING", "auto") != "off":
        try:
            gate = WakeGate()
        except FileNotFoundError as exc:
            logger.error("wake model unavailable (set NARADA_WAKE_GATING=off "
                         "to bypass): %s", exc)
            bail("wake model unavailable")
            return
        if not await _wait_for_wake(ctx, gate, closed):
            bail("room/audio ended without wake")
            return

    if closed.is_set():
        bail("room closed before admission")
        return
    if not ctx.room.remote_participants:
        # Dispatch-on-room-creation can beat the creating participant's
        # own join (observed with the BOX-3: agent connects first, sees
        # an empty room). Wait briefly for them instead of refusing.
        joined = asyncio.Event()
        ctx.room.on("participant_connected", lambda *_: joined.set())
        if ctx.room.remote_participants:  # re-check after handler attach
            joined.set()
        try:
            await asyncio.wait_for(joined.wait(), timeout=60)
            logger.info("participant arrived — proceeding to admission")
        except asyncio.TimeoutError:
            bail("no participant within 60s")
            return
    if closed.is_set():
        bail("room closed while waiting for participant")
        return

    # Admission: reserve this session's maximum charge (fail closed on
    # budget exhaustion, races, or a damaged ledger).
    try:
        reservation = budget.reserve()
    except (BudgetExceeded, BudgetUnavailable) as exc:
        logger.warning("refusing session: %s", exc)
        bail("budget refused")
        return

    if closed.is_set() or not ctx.room.remote_participants:
        budget.settle(reservation, 0.0)
        bail("room emptied during admission — reservation released")
        return

    started = time.monotonic()
    session = None
    transcript = None
    try:
        session = AgentSession(
            llm=lk_openai.realtime.RealtimeModel(model=REALTIME_MODEL),
        )
        # Full transcript of what was said, both sides, to
        # ~/.narada/heartbeat/voice-transcripts/. Fail-open.
        transcript = attach(session, ctx.room.name)
        agent = Agent(instructions=INSTRUCTIONS, tools=build_voice_tools())
        await session.start(agent=agent, room=ctx.room)
        logger.info("realtime session open (model=%s)", REALTIME_MODEL)
        # cap vs. natural close, whichever first — a disconnected room
        # must stop billing immediately, not at the cap
        try:
            await asyncio.wait_for(
                closed.wait(), timeout=budget.session_cap_s
            )
            logger.info("room closed — ending session")
        except asyncio.TimeoutError:
            logger.info("session cap reached (%.0fs) — closing",
                        budget.session_cap_s)
    finally:
        if transcript is not None:
            transcript.close("session ended")
        if session is not None:
            try:
                await session.aclose()
            except Exception as exc:
                logger.warning("session close failed: %s", exc)
        budget.settle(reservation, time.monotonic() - started)
        # Leave the room when the session ends. Without this the job
        # lingers as a zombie participant, holding the room open so no
        # fresh job can ever be dispatched (observed with the BOX-3).
        ctx.shutdown(reason="session ended")


async def _wait_for_wake(
    ctx: JobContext, gate: WakeGate, closed: "asyncio.Event"
) -> bool:
    """Stream the first participant's mic into the gate.

    Returns True only on an affirmative detection; False when the room
    closes first, no audio track ever arrives, or the stream ends —
    the caller must NOT open a billed session on False.
    """
    import asyncio

    track_q: "agents.utils.aio.Chan[rtc.RemoteAudioTrack]" = agents.utils.aio.Chan()

    def _on_track(track: rtc.Track, *_args) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            track_q.send_nowait(track)

    ctx.room.on("track_subscribed", _on_track)
    for participant in ctx.room.remote_participants.values():
        for pub in participant.track_publications.values():
            if pub.track and pub.track.kind == rtc.TrackKind.KIND_AUDIO:
                track_q.send_nowait(pub.track)

    recv_task = asyncio.ensure_future(track_q.recv())
    closed_task = asyncio.ensure_future(closed.wait())
    done, _ = await asyncio.wait(
        {recv_task, closed_task}, return_when=asyncio.FIRST_COMPLETED
    )
    if closed_task in done:
        recv_task.cancel()
        return False
    closed_task.cancel()
    track = recv_task.result()
    logger.info("wake gate armed — listening for 'Narada'")
    detected = False
    stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
    try:
        async for event in stream:
            samples = (
                np.frombuffer(event.frame.data, dtype=np.int16)
                .astype(np.float32) / 32768.0
            )
            if gate.feed(samples) is not None:
                detected = True
                break
    finally:
        await stream.aclose()
    if detected:
        logger.info("wake detected — opening realtime session")
    return detected


HEALTH_HOST = "127.0.0.1"
HEALTH_PORT = int(os.environ.get("NARADA_VOICE_HEALTH_PORT", "8792"))


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    _check_env()
    # Fixed health port so the host supervisor can probe liveness at a
    # known URL (cross-review #6). The agents worker's `/` returns 503 on
    # a lost LiveKit connection, and an event-loop hang makes the probe
    # time out — either way the supervisor restarts it.
    agents.cli.run_app(WorkerOptions(
        entrypoint_fnc=entrypoint,
        host=HEALTH_HOST,
        port=HEALTH_PORT,
    ))


if __name__ == "__main__":
    main()
