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

from prana.voice.budget import BudgetExceeded, VoiceBudget
from prana.voice.tools import build_voice_tools
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
    missing = [
        k for k in ("OPENAI_API_KEY", "LIVEKIT_URL", "LIVEKIT_API_KEY",
                    "LIVEKIT_API_SECRET")
        if not os.environ.get(k)
    ]
    if missing:
        raise SystemExit(
            f"voice worker missing env: {', '.join(missing)} "
            f"(OPENAI_API_KEY is Suti's gate; LiveKit vars come from "
            f"~/.narada/.livekit.env)"
        )


async def entrypoint(ctx: JobContext) -> None:
    budget = VoiceBudget()
    try:
        budget.check_can_start()
    except BudgetExceeded as exc:
        logger.warning("refusing session: %s", exc)
        return

    await ctx.connect()

    # Wake gating: watch LAN audio locally; the billed realtime session
    # opens only after "Narada". NARADA_WAKE_GATING=off skips it (the
    # browser-mic milestone predates the trained model).
    if os.environ.get("NARADA_WAKE_GATING", "auto") != "off":
        try:
            gate = WakeGate()
            await _wait_for_wake(ctx, gate)
        except FileNotFoundError as exc:
            logger.warning("wake gating disabled: %s", exc)

    started = time.monotonic()

    session = AgentSession(
        llm=lk_openai.realtime.RealtimeModel(model=REALTIME_MODEL),
    )
    agent = Agent(instructions=INSTRUCTIONS, tools=build_voice_tools())
    await session.start(agent=agent, room=ctx.room)
    logger.info("realtime session open (model=%s)", REALTIME_MODEL)

    try:
        # duration cap: hard-close the billed session when time is up
        cap = budget.session_cap_s
        while time.monotonic() - started < cap:
            await agents.utils.aio.sleep(5)
        logger.info("session cap reached (%.0fs) — closing", cap)
    finally:
        budget.record_session(time.monotonic() - started)
        await session.aclose()


async def _wait_for_wake(ctx: JobContext, gate: WakeGate) -> None:
    """Stream the first participant's mic into the gate until wake."""
    track_q: "agents.utils.aio.Chan[rtc.RemoteAudioTrack]" = agents.utils.aio.Chan()

    def _on_track(track: rtc.Track, *_args) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            track_q.send_nowait(track)

    ctx.room.on("track_subscribed", _on_track)
    for participant in ctx.room.remote_participants.values():
        for pub in participant.track_publications.values():
            if pub.track and pub.track.kind == rtc.TrackKind.KIND_AUDIO:
                track_q.send_nowait(pub.track)

    track = await track_q.recv()
    logger.info("wake gate armed — listening for 'Narada'")
    stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
    async for event in stream:
        samples = (
            np.frombuffer(event.frame.data, dtype=np.int16).astype(np.float32)
            / 32768.0
        )
        if gate.feed(samples) is not None:
            break
    await stream.aclose()
    logger.info("wake detected — opening realtime session")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    _check_env()
    agents.cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))


if __name__ == "__main__":
    main()
