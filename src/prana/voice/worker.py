"""The voice worker — LiveKit Agents entrypoint.

Run: ``python -m prana.voice.worker dev`` (or ``start`` under the host).
Needs OPENAI_API_KEY and ~/.narada/.livekit.env. Wake-gated: the room's
audio is watched locally; the realtime session (billed) exists only
between wake and hangup/timeout.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions
from livekit.plugins import openai as lk_openai

from prana.voice.admission import (
    DEVICE_IDENTITY,
    TIER_PERSONAL,
    TIER_SHAREABLE,
    TOPIC_ADMISSION,
    TOPIC_SESSION,
    TapAdmission,
    is_sleep_tap,
)
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
    """One long-lived job per room, looping wake-watch → session →
    wake-watch (M2 spec). Admission into a *billed* session happens by
    wake-word detection OR a verified tap assertion; the room/audio
    connection itself is LAN-only and free."""
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

    # ── Tap admission + data-channel plumbing (M2 spec §2.2) ─────────
    admission = TapAdmission()
    tap_admit = asyncio.Event()
    sleep_tap = asyncio.Event()
    box_dropped = asyncio.Event()
    state = {"assertion": None, "in_session": False}

    def _on_data(packet) -> None:
        try:
            ident = getattr(getattr(packet, "participant", None),
                            "identity", None)
            data = getattr(packet, "data", b"")
            if state["in_session"]:
                if is_sleep_tap(ident, data):
                    sleep_tap.set()
                return
            assertion = admission.verify(ident, data)
            if assertion is not None:
                state["assertion"] = assertion
                tap_admit.set()
        except Exception:  # data handler must never take the job down
            pass

    ctx.room.on("data_received", _on_data)

    def _on_participant_left(participant, *_args) -> None:
        if getattr(participant, "identity", None) == DEVICE_IDENTITY:
            box_dropped.set()

    ctx.room.on("participant_disconnected", _on_participant_left)

    async def _publish(topic: str, obj: dict) -> None:
        try:
            await ctx.room.local_participant.publish_data(
                json.dumps(obj), topic=topic,
                destination_identities=[DEVICE_IDENTITY],
            )
        except Exception as exc:
            logger.debug("publish %s failed: %s", topic, exc)

    # Wake gating: watch LAN audio locally; the billed realtime session
    # opens only after "Narada" OR a verified tap. FAIL CLOSED on a
    # missing model. NARADA_WAKE_GATING=off (dev/browser-mic mode) keeps
    # the pre-M2 single-session behavior.
    gating = os.environ.get("NARADA_WAKE_GATING", "auto") != "off"
    gate = None
    if gating:
        try:
            gate = WakeGate()
        except FileNotFoundError as exc:
            logger.error("wake model unavailable (set NARADA_WAKE_GATING=off "
                         "to bypass): %s", exc)
            bail("wake model unavailable")
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

    async def _run_session(tier: str) -> str:
        """One billed session. Returns the end reason."""
        try:
            reservation = budget.reserve()
        except (BudgetExceeded, BudgetUnavailable) as exc:
            logger.warning("refusing session: %s", exc)
            return "budget-refused"
        if closed.is_set() or not ctx.room.remote_participants:
            budget.settle(reservation, 0.0)
            return "room-emptied-during-admission"

        started = time.monotonic()
        session = None
        transcript = None
        reason = "unknown"
        state["in_session"] = True
        sleep_tap.clear()
        box_dropped.clear()
        try:
            session = AgentSession(
                llm=lk_openai.realtime.RealtimeModel(model=REALTIME_MODEL),
            )
            transcript = attach(session, ctx.room.name)
            agent = Agent(instructions=INSTRUCTIONS,
                          tools=build_voice_tools())
            await session.start(agent=agent, room=ctx.room)
            logger.info("realtime session open (model=%s, tier=%s)",
                        REALTIME_MODEL, tier)
            await _publish(TOPIC_SESSION,
                           {"type": "session", "open": True, "tier": tier})
            # End on whichever comes first: room closed, sleep tap, the
            # cap — and for PERSONAL tier, the device dropping (the tier
            # must not survive a reconnect, per spec §2.2).
            waiters = {
                "room-closed": asyncio.ensure_future(closed.wait()),
                "sleep-tap": asyncio.ensure_future(sleep_tap.wait()),
            }
            if tier == TIER_PERSONAL:
                waiters["device-dropped"] = asyncio.ensure_future(
                    box_dropped.wait())
            done, pending = await asyncio.wait(
                waiters.values(),
                timeout=budget.session_cap_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            if not done:
                reason = "session-cap"
            else:
                reason = next(k for k, v in waiters.items() if v in done)
            logger.info("session ending: %s", reason)
        finally:
            state["in_session"] = False
            if transcript is not None:
                transcript.close(reason)
            if session is not None:
                try:
                    await session.aclose()
                except Exception as exc:
                    logger.warning("session close failed: %s", exc)
            budget.settle(reservation, time.monotonic() - started)
            await _publish(TOPIC_SESSION,
                           {"type": "session", "open": False,
                            "reason": reason})
        return reason

    if not gating:
        # Dev/browser-mic mode: pre-M2 behavior — one immediate session,
        # then the job ends. (Looping here would chain billed sessions
        # back-to-back against an always-present device.)
        await _run_session(TIER_SHAREABLE)
        ctx.shutdown(reason="session ended (dev mode)")
        return

    # ── The wake-watch loop (M2): watch → admit → session → repeat ───
    while not closed.is_set():
        tap_admit.clear()
        state["assertion"] = None
        nonce = admission.new_cycle()
        await _publish(TOPIC_ADMISSION,
                       {"type": "admission_nonce", "nonce": nonce})
        logger.info("wake-watch: listening for wake word or tap")

        wake_task = asyncio.ensure_future(_wait_for_wake(ctx, gate, closed))
        tap_task = asyncio.ensure_future(tap_admit.wait())
        closed_task = asyncio.ensure_future(closed.wait())
        done, pending = await asyncio.wait(
            {wake_task, tap_task, closed_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        admission.invalidate()  # one admission decision per cycle

        if closed_task in done:
            break
        if tap_task in done and state["assertion"] is not None:
            tier = state["assertion"].tier
            logger.info("admitted by tap (tier=%s)", tier)
        elif wake_task in done and not wake_task.cancelled() \
                and wake_task.result():
            tier = TIER_SHAREABLE
            logger.info("admitted by wake word")
        else:
            # audio stream ended without a wake — device likely flapped;
            # next cycle re-arms (the room-grace handles true departure)
            await asyncio.sleep(1.0)
            continue

        end_reason = await _run_session(tier)
        if end_reason in ("budget-refused",):
            # fail-closed but not silently: stay in wake-watch, a later
            # cycle may succeed (e.g. next day’s budget)
            await asyncio.sleep(30.0)

    ctx.shutdown(reason="room closed — job ended")


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
