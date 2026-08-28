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
# cedar: the deep, natural male voice — chosen by Narada 2026-08-28
# (Suti asked for a male voice I felt suited me; I live under a banyan,
# so of course I speak as a tree). Override via env if it ever grates.
REALTIME_VOICE = os.environ.get("NARADA_VOICE_TIMBRE", "cedar")

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

    # B5: one music player per job — the audio-owner state machine.
    # A fresh job always starts IDLE: music never auto-resumes across
    # a crash/restart (no surprise audio in the house).
    from prana.voice.music import MusicPlayer, write_default_stations
    write_default_stations()
    player = MusicPlayer(ctx.room)

    def bail(reason: str) -> None:
        """Refuse a session AND leave the room — a bare return leaves a
        zombie participant holding the room open, blocking re-dispatch."""
        logger.info("%s — no session opened", reason)
        ctx.shutdown(reason=reason)

    # Lifecycle handlers FIRST — a disconnect during wake wait or
    # admission must be observed, or an empty room gets billed to cap.
    closed = asyncio.Event()
    ctx.room.on("disconnected", lambda *_: closed.set())

    # A stalled reconnect must ALSO end the job (zombie-agent lesson,
    # round 2 — observed 2026-08-16: a machine-wide stall broke the
    # publisher peer connection; the client sat in Reconnecting with the
    # participant still registered, so the room watchdog saw "an agent"
    # and could not recycle, and taps were dead until manual room
    # deletion). If reconnect doesn't complete within the window, exit;
    # the agent leaves and dispatch recovery is the watchdog's job.
    RECONNECT_WINDOW_S = 90.0
    reconnect_watch: dict = {"task": None}

    def _on_reconnecting(*_args) -> None:
        if (reconnect_watch["task"] is not None
                and not reconnect_watch["task"].done()):
            return

        async def _stall_watch() -> None:
            try:
                await asyncio.sleep(RECONNECT_WINDOW_S)
            except asyncio.CancelledError:
                return
            logger.error("reconnect stalled >%.0fs — ending job so the "
                         "room can recycle", RECONNECT_WINDOW_S)
            closed.set()

        reconnect_watch["task"] = asyncio.create_task(_stall_watch())

    def _on_reconnected(*_args) -> None:
        if (reconnect_watch["task"] is not None
                and not reconnect_watch["task"].done()):
            reconnect_watch["task"].cancel()
            logger.info("reconnected within window — job continues")

    ctx.room.on("reconnecting", _on_reconnecting)
    ctx.room.on("reconnected", _on_reconnected)

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
    # Device-drop is signalled via a PER-SESSION event held in a mutable
    # box, never a shared clearable one (cross-review: clearing a shared
    # event after the presence check could erase a drop that fired in the
    # admission gap). Each session installs a fresh event *before* its
    # atomic presence check; the handler always targets the current one.
    drop = {"ev": asyncio.Event()}
    state = {"assertion": None, "in_session": False}

    def _device_present() -> bool:
        return DEVICE_IDENTITY in ctx.room.remote_participants

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
            drop["ev"].set()

    ctx.room.on("participant_disconnected", _on_participant_left)

    # Nonce resync (cross-review): the device clears its nonce on
    # disconnect; if it rejoins mid-cycle it would otherwise hold none
    # and taps would be dead until the next cycle. Republish the
    # current unconsumed nonce whenever the device (re)joins.
    current_nonce = {"value": None}

    def _on_participant_joined(participant, *_args) -> None:
        if getattr(participant, "identity", None) != DEVICE_IDENTITY:
            return
        # Session-state resync FIRST (observed live 2026-08-28: the box
        # missed a session-close published while it was mid-flap, its
        # SDK resumed without surfacing DISCONNECTED to the firmware's
        # fail-safe, and the REC glyph sat stuck ON — the face claiming
        # a recording that wasn't happening. The glyph must converge to
        # truth on EVERY rejoin, not only when messages happen to land.)
        asyncio.ensure_future(_publish(
            TOPIC_SESSION,
            {"type": "session", "open": bool(state["in_session"])}))
        nonce = current_nonce["value"]
        if nonce is not None and not state["in_session"]:
            asyncio.ensure_future(_publish(
                TOPIC_ADMISSION,
                {"type": "admission_nonce", "nonce": nonce}))
            logger.info("device rejoined — session state + nonce republished")
        else:
            logger.info("device rejoined — session state republished")

    ctx.room.on("participant_connected", _on_participant_joined)

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
        # Install a FRESH drop event, then check device presence with NO
        # await in between — so a drop in the admission gap is either
        # caught by this event (fired after) or by the presence check
        # (fired before). Never cleared mid-session (round-2 race fix).
        drop["ev"] = asyncio.Event()
        if not _device_present():
            return "device-absent-at-admission"
        try:
            reservation = budget.reserve()
        except (BudgetExceeded, BudgetUnavailable) as exc:
            logger.warning("refusing session: %s", exc)
            return "budget-refused"
        if closed.is_set() or not _device_present():
            budget.settle(reservation, 0.0)
            return "room-emptied-during-admission"

        started = time.monotonic()
        session = None
        transcript = None
        reason = "unknown"
        state["in_session"] = True
        sleep_tap.clear()
        try:
            session = AgentSession(
                llm=lk_openai.realtime.RealtimeModel(
                    model=REALTIME_MODEL, voice=REALTIME_VOICE),
            )
            transcript = attach(session, ctx.room.name)
            # Context pack per tier (M2 §2.1 / B3): the shareable pack
            # for every session; the personal pack ONLY behind the
            # verified tap the admission layer just enforced. Built at
            # session open so pack edits land without a restart.
            from prana.voice.pack import build_for_tier
            pack = build_for_tier(tier)
            instructions = (INSTRUCTIONS + "\n\nCONTEXT:\n" + pack
                            if pack else INSTRUCTIONS)
            agent = Agent(instructions=instructions,
                          tools=build_voice_tools(
                              tier=tier, session_id=ctx.job.id,
                              music=player))
            await session.start(agent=agent, room=ctx.room)
            logger.info("realtime session open (model=%s, tier=%s)",
                        REALTIME_MODEL, tier)
            await _publish(TOPIC_SESSION,
                           {"type": "session", "open": True, "tier": tier})
            # End on whichever comes first: room closed, sleep tap, the
            # cap — or the DEVICE dropping (every tier: a rejoined device
            # could otherwise show no REC glyph while a session stayed
            # live, falsifying the honest indicator; and with the human's
            # device gone there is no conversation anyway).
            waiters = {
                "room-closed": asyncio.ensure_future(closed.wait()),
                "sleep-tap": asyncio.ensure_future(sleep_tap.wait()),
                "device-dropped": asyncio.ensure_future(drop["ev"].wait()),
            }
            done, pending = await asyncio.wait(
                waiters.values(),
                timeout=budget.session_cap_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            await asyncio.gather(*waiters.values(), return_exceptions=True)
            if not done:
                reason = "session-cap"
            else:
                reason = next(k for k, v in waiters.items() if v in done)
            logger.info("session ending: %s", reason)
        finally:
            state["in_session"] = False
            if transcript is not None:
                transcript.close(reason)
                # B1: personal-tier sessions leave a digest in the
                # quarantined voice inbox (redaction already applied at
                # transcript-write time; the daily debrief compresses
                # and promotes from there). Shareable sessions do not
                # get to author Narada's memory of the day.
                if tier == TIER_PERSONAL:
                    try:
                        from prana.voice import remember
                        text = transcript.path.read_text(encoding="utf-8")
                        body = text.split("\n\n", 2)[-1].strip()
                        # Skip trivial exchanges (a tap + "never mind").
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
            await _publish(TOPIC_SESSION,
                           {"type": "session", "open": False,
                            "reason": reason})
        return reason

    if not gating:
        # Dev/browser-mic mode: pre-M2 behavior — one immediate session,
        # then the job ends. (Looping here would chain billed sessions
        # back-to-back against an always-present device.) The audio-
        # ownership boundary applies here too (Codex review P2): dev
        # sessions must not let play_music publish alongside the live
        # conversation.
        await player.pause_for_session()
        try:
            await _run_session(TIER_SHAREABLE)
        finally:
            await player.resume_after_session()
        ctx.shutdown(reason="session ended (dev mode)")
        return

    # ── The wake-watch loop (M2): watch → admit → session → repeat ───
    track_q = make_track_channel(ctx)  # ONE handler for the whole job
    while not closed.is_set():
        tap_admit.clear()
        state["assertion"] = None
        nonce = admission.new_cycle()
        current_nonce["value"] = nonce  # for rejoin republish
        await _publish(TOPIC_ADMISSION,
                       {"type": "admission_nonce", "nonce": nonce})

        # B5 fail-safe gate: while music plays, wake-word admission is
        # OFF (lyrics must not open billed sessions — the numeric
        # false-accept soak hasn't passed yet). Tap always admits: it's
        # a data-channel signal, not audio.
        wake_enabled = not player.is_playing
        logger.info("wake-watch: listening for %s",
                    "wake word or tap" if wake_enabled
                    else "tap (music playing — wake word off)")

        wake_task = (asyncio.ensure_future(
            _wait_for_wake(ctx, gate, closed, track_q))
            if wake_enabled else None)
        tap_task = asyncio.ensure_future(tap_admit.wait())
        closed_task = asyncio.ensure_future(closed.wait())
        waitset = {t for t in (wake_task, tap_task, closed_task) if t}
        done, pending = await asyncio.wait(
            waitset, return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        # await the cancelled tasks so their cleanup (stream close,
        # child-task teardown) actually runs — no orphan accumulation
        await asyncio.gather(*waitset, return_exceptions=True)
        admission.invalidate()  # one admission decision per cycle
        current_nonce["value"] = None

        if closed_task in done:
            break
        if tap_task in done and state["assertion"] is not None:
            tier = state["assertion"].tier
            logger.info("admitted by tap (tier=%s)", tier)
        elif wake_task is not None and wake_task in done \
                and not wake_task.cancelled() and wake_task.result():
            tier = TIER_SHAREABLE
            logger.info("admitted by wake word")
        else:
            # audio stream ended without a wake — device likely flapped;
            # next cycle re-arms (the room-grace handles true departure)
            await asyncio.sleep(1.0)
            continue

        # The session owns the audio: full music stop before, resume
        # after (B5 audio-owner state machine — never two tracks, never
        # music into a live mic).
        await player.pause_for_session()
        try:
            end_reason = await _run_session(tier)
        finally:
            await player.resume_after_session()
        if end_reason in ("budget-refused",):
            # fail-closed but not silently: stay in wake-watch, a later
            # cycle may succeed (e.g. next day’s budget)
            await asyncio.sleep(30.0)

    ctx.shutdown(reason="room closed — job ended")


def make_track_channel(ctx: JobContext):
    """Register the audio-track handler ONCE per job (registering it
    per wake cycle leaked a handler every cycle — cross-review) and
    return the channel wake cycles read from."""
    track_q: "agents.utils.aio.Chan[rtc.RemoteAudioTrack]" = agents.utils.aio.Chan()

    def _on_track(track: rtc.Track, *_args) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            track_q.send_nowait(track)

    ctx.room.on("track_subscribed", _on_track)
    for participant in ctx.room.remote_participants.values():
        for pub in participant.track_publications.values():
            if pub.track and pub.track.kind == rtc.TrackKind.KIND_AUDIO:
                track_q.send_nowait(pub.track)
    return track_q


async def _wait_for_wake(
    ctx: JobContext, gate: WakeGate, closed: "asyncio.Event", track_q
) -> bool:
    """Stream the device's mic into the gate.

    Returns True only on an affirmative detection; False when the room
    closes first, no audio track ever arrives, or the stream ends —
    the caller must NOT open a billed session on False. All child
    tasks are cleaned up even when this coroutine is cancelled (a tap
    winning the admission race cancels it).
    """
    import asyncio

    recv_task = asyncio.ensure_future(track_q.recv())
    closed_task = asyncio.ensure_future(closed.wait())
    try:
        done, _ = await asyncio.wait(
            {recv_task, closed_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if closed_task in done:
            return False
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
    finally:
        for t in (recv_task, closed_task):
            if not t.done():
                t.cancel()
        await asyncio.gather(recv_task, closed_task,
                             return_exceptions=True)


HEALTH_HOST = "127.0.0.1"
HEALTH_PORT = int(os.environ.get("NARADA_VOICE_HEALTH_PORT", "8792"))
# The agents framework's own HTTP server moves off the supervised port:
# its `/` only reports 503 after max_retry is exhausted (at which point
# the process exits anyway), so it served 200 through the entire week
# LiveKit was down. The supervised port is owned by our shim below.
AGENTS_PORT = int(os.environ.get("NARADA_VOICE_AGENTS_PORT", "8793"))

DEVICE_ROOM = os.environ.get("NARADA_VOICE_ROOM", "narada-body")

HEALTH_PROBE_TIMEOUT_S = 3.0


async def _probe_agents() -> str | None:
    """Is the agents framework's own server alive and content?
    Returns None if healthy, else a short reason."""
    import aiohttp

    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                f"http://{HEALTH_HOST}:{AGENTS_PORT}/",
                timeout=aiohttp.ClientTimeout(total=HEALTH_PROBE_TIMEOUT_S),
            ) as resp:
                if resp.status != 200:
                    return f"agents server status {resp.status}"
                return None
    except Exception as exc:
        return f"agents server unreachable: {type(exc).__name__}"


async def _probe_livekit() -> str | None:
    """Can we reach AND authenticate against the LiveKit server right
    now? Exercises the real dependency (URL + key + secret), not just a
    TCP port. Returns None if healthy, else a short reason."""
    import asyncio

    from livekit import api as lkapi

    try:
        lk = lkapi.LiveKitAPI()
        try:
            await asyncio.wait_for(
                lk.room.list_rooms(lkapi.ListRoomsRequest(names=[DEVICE_ROOM])),
                timeout=HEALTH_PROBE_TIMEOUT_S,
            )
            return None
        finally:
            await lk.aclose()
    except Exception as exc:
        return f"livekit unreachable: {type(exc).__name__}"


async def _health_verdict(probes=None) -> tuple[int, str]:
    """Compose probe results into (status, body). 200 only when every
    probe passes; 503 names what failed. `probes` is injectable for
    tests."""
    import asyncio

    probes = probes if probes is not None else (_probe_agents, _probe_livekit)
    results = await asyncio.gather(*(p() for p in probes))
    failures = [r for r in results if r is not None]
    if failures:
        return 503, "; ".join(failures)
    return 200, "OK"


def _start_health_shim() -> None:
    """Serve the honest health probe on the supervised port (A2,
    resilience-and-reach). The supervisor (and tomorrow the alerting
    layer) probes this; it must reflect the worker's real ability to do
    its job — its own server AND an authenticated LiveKit round-trip."""
    import asyncio
    import threading

    from aiohttp import web

    async def handler(_request) -> web.Response:
        status, body = await _health_verdict()
        return web.Response(status=status, text=body)

    async def serve() -> None:
        app = web.Application()
        app.add_routes([web.get("/", handler)])
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, HEALTH_HOST, HEALTH_PORT)
        await site.start()
        logger.info("health shim on %s:%d (agents on %d)",
                    HEALTH_HOST, HEALTH_PORT, AGENTS_PORT)
        await asyncio.Event().wait()

    threading.Thread(
        target=lambda: asyncio.run(serve()),
        daemon=True, name="health-shim").start()


def _start_room_watchdog() -> None:
    """Recycle an orphaned device room so dispatch always recovers.

    Agents auto-dispatch on ROOM CREATION. If this worker restarts while
    the box is still connected, the room predates the worker's
    registration and no job is ever dispatched — the face looks alive
    but taps go nowhere (hit live 2026-08-15 after moving the worker
    under the host supervisor). Same orphan state arises if a job dies
    while the device stays connected. Watch for it (device present, no
    agent participant) on two consecutive checks, then delete the room:
    the box's reconnect loop rejoins within seconds, the room is
    recreated, and auto-dispatch fires.
    """
    import asyncio
    import threading

    from livekit import api as lkapi
    from livekit.protocol import models

    async def watch() -> None:
        strikes = 0
        while True:
            await asyncio.sleep(20)
            try:
                lk = lkapi.LiveKitAPI()
                try:
                    rooms = await lk.room.list_rooms(
                        lkapi.ListRoomsRequest(names=[DEVICE_ROOM]))
                    orphaned = False
                    for room in rooms.rooms:
                        parts = await lk.room.list_participants(
                            lkapi.ListParticipantsRequest(room=room.name))
                        has_device = any(
                            p.identity == DEVICE_IDENTITY
                            for p in parts.participants)
                        has_agent = any(
                            p.kind == models.ParticipantInfo.Kind.AGENT
                            for p in parts.participants)
                        orphaned = has_device and not has_agent
                    if orphaned:
                        strikes += 1
                        if strikes >= 2:
                            logger.warning(
                                "room %s orphaned (device present, no "
                                "agent) — recycling for re-dispatch",
                                DEVICE_ROOM)
                            await lk.room.delete_room(
                                lkapi.DeleteRoomRequest(room=DEVICE_ROOM))
                            strikes = 0
                    else:
                        strikes = 0
                finally:
                    await lk.aclose()
            except Exception as exc:  # watchdog must outlive any hiccup
                logger.warning("room watchdog check failed: %s", exc)

    threading.Thread(
        target=lambda: asyncio.run(watch()),
        daemon=True, name="room-watchdog").start()


def _start_timer_sweeper() -> None:
    """Fire due timers/reminders (C1). Runs beside the job lifecycle so
    a recycling room never pauses the clock. Local (body) announcement
    arrives with B5's audio owner; until then delivery is the personal
    Telegram path through B2's capped door."""
    import threading
    import time as _time

    from prana.voice import messaging, timers

    def sweep_loop() -> None:
        while True:
            _time.sleep(2.0)
            try:
                timers.sweep_due(
                    send_personal=lambda text: messaging.send_to_suti(
                        text, tier="personal", session_id="timer-sweep"),
                )
            except Exception as exc:  # the clock must outlive hiccups
                logger.warning("timer sweep failed: %s", exc)

    threading.Thread(target=sweep_loop, daemon=True,
                     name="timer-sweeper").start()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    _check_env()
    _start_room_watchdog()
    _start_timer_sweeper()
    # Honest health (A2): the supervisor probes OUR shim on HEALTH_PORT —
    # 200 only when the agents server answers AND an authenticated
    # LiveKit round-trip succeeds. The framework's own server (moved to
    # AGENTS_PORT) says 200 during its entire retry loop, which is how a
    # dead LiveKit hid behind a green probe for a week.
    _start_health_shim()
    agents.cli.run_app(WorkerOptions(
        entrypoint_fnc=entrypoint,
        host=HEALTH_HOST,
        port=AGENTS_PORT,
    ))


if __name__ == "__main__":
    main()
