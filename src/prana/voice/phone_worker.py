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
HEALTH_PORT = int(os.environ.get("AKHADA_VOICE_HEALTH_PORT", "8796"))

PHONE_NOTE = """

PHONE: he opened Akhada on his phone and tapped talk — he may be at the
gym, cooking, or out walking, so keep replies even shorter than usual
and confirm logged entries with a quick read-back. This surface is for
food, training, weight and how the day is going; anything deeper, note
it and keep moving."""

SILENCE_END_S = 75.0
EMPTY_GRACE_S = 30.0
# Seconds after a FINAL user transcript with no agent response before
# the worker forces one. Field pattern (2026-09-07 transcript): Suti
# lists items with pauses, each fragment's turn-end starts a response,
# the next fragment interrupts it, and after the LAST fragment nothing
# regenerates — 47s of "listening" until he said "Hello?".
REPLY_NUDGE_S = float(os.environ.get("NARADA_PHONE_REPLY_NUDGE_S", "6"))


def context_pack(metadata: str | None, *, max_turns: int = 12,
                 max_chars: int = 320) -> str:
    """The brain session's recent typed turns, for the call's
    instructions (plan §T3a — one-way, read-only). The dispatch
    metadata names {session_id, tier}; the transcript lives at
    ~/.narada/brain/sessions/<tier>/<session_id>/transcript.jsonl.
    Absent, malformed, or foreign → empty string, never an error."""
    import json as _json
    from pathlib import Path

    try:
        meta = _json.loads(metadata or "")
        session_id, tier = str(meta["session_id"]), str(meta["tier"])
    except (ValueError, KeyError, TypeError):
        return ""
    if not session_id or not tier or "/" in session_id or "\\" in session_id:
        return ""
    path = (Path.home() / ".narada" / "brain" / "sessions" / tier / session_id
            / "transcript.jsonl")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    turns: list[str] = []
    for line in lines:
        try:
            e = _json.loads(line)
        except ValueError:
            continue
        role, text = e.get("role"), str(e.get("content", "")).strip()
        if role not in ("user", "assistant") or not text:
            continue
        who = "Suti" if role == "user" else "Narada"
        turns.append(f"{who}: {text[:max_chars]}{'…' if len(text) > max_chars else ''}")
    turns = turns[-max_turns:]
    if not turns:
        return ""
    return ("RECENT TYPED CHAT with Suti in this same conversation — the "
            "same person, continue from here; do not re-ask what is "
            "already settled:\n" + "\n".join(turns))


BRIEF_NOTE = (
    "BRIEF MODE is on. Suti asked for short answers on this phone: reply "
    "in one or two short sentences, no preamble, no recap, no flourish, "
    "no offers of more. If a question truly needs more, give the one-line "
    "answer first and ask whether he wants the rest. Stay brief until he "
    "says otherwise or turns brief mode off."
)


NUDGE_COOLDOWN_S = 3.0


def _speech_busy(handle: object) -> bool:
    """A SpeechHandle that has not finished — generation pending or audio
    still playing. Duck-typed: agents' SpeechHandle exposes done()."""
    if handle is None:
        return False
    done = getattr(handle, "done", None)
    try:
        return not done() if callable(done) else False
    except Exception:  # noqa: BLE001
        return False


def nudge_allowed(now: float, last: float, own_handle: object,
                  current_speech: object) -> bool:
    """One nudge at a time (review): not within the cool-down, not while
    the previous nudge's reply is pending or playing, and not while
    Narada is already speaking — a queued invitation would fire AFTER
    the answer as unsolicited follow-up speech."""
    if now - last < NUDGE_COOLDOWN_S:
        return False
    if _speech_busy(own_handle) or _speech_busy(current_speech):
        return False
    return True


def load_voice_prefs() -> dict:
    """Operator-tunable voice knobs, written by the phone app through
    the brain (POST /api/voice/prefs). Read at job start so a new call
    picks up changes without a worker restart. Absent file = defaults."""
    from prana.voice.prefs import read_prefs

    return read_prefs()


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

        # First field call (2026-09-07): stock VAD (threshold 0.5)
        # fired on random handset noise — each false turn burned a full
        # ~7.5k-token response against the 40k TPM ceiling, and noise
        # bursts came back transcribed in random languages. Raised
        # threshold (community-effective range 0.6–0.75), near-field
        # noise reduction (handset, not speakerphone), and an explicit
        # language pin address all three. Prefs file (set from the
        # phone app's Settings) overrides env, env overrides defaults.
        prefs = load_voice_prefs()
        vad_threshold = float(prefs.get("vad_threshold") or os.environ.get(
            "NARADA_PHONE_VAD_THRESHOLD", "0.7"))
        noise_pref = str(prefs.get("noise_reduction") or "near_field")
        language = str(prefs.get("language") or os.environ.get(
            "NARADA_PHONE_LANGUAGE", "en"))
        # The model too, from Settings (Suti 2026-09-09); env/default
        # otherwise. Read at job start like the rest: next call.
        realtime_model = str(prefs.get("model") or box.REALTIME_MODEL)
        session = AgentSession(
            llm=lk_openai.realtime.RealtimeModel(
                model=realtime_model, voice=box.REALTIME_VOICE,
                turn_detection=_rt_mod.TurnDetection(
                    type="server_vad",
                    threshold=vad_threshold,
                    prefix_padding_ms=300,
                    silence_duration_ms=700),
                input_audio_noise_reduction=(
                    None if noise_pref == "off" else noise_pref),
                input_audio_transcription=_rt_mod.InputAudioTranscription(
                    model="gpt-4o-mini-transcribe",
                    language=language)),
            user_away_timeout=None,
        )
        logger.info("voice prefs: vad=%.2f noise=%s lang=%s model=%s",
                    vad_threshold, noise_pref, language, realtime_model)

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
        pack_text = context_pack(getattr(ctx.job, "metadata", None))
        if pack_text:
            instructions += "\n\n" + pack_text
            logger.info("context pack: %d chars from the chat session", len(pack_text))
        # Brief mode (Suti on the phone 2026-09-08: "let's have a brief
        # mode… toggle it on and off"): a pref like VAD, flipped from
        # Settings or by voice through the brief_mode tool.
        if str(prefs.get("brevity") or "normal") == "brief":
            instructions += "\n\n" + BRIEF_NOTE
            logger.info("brief mode on")

        async def _session_speak(inst: str) -> None:
            # The rest timer's ring (akhada gym-session G4): a proactive
            # spoken line in THIS session, same mechanism as the greeting.
            handle = session.generate_reply(instructions=inst)
            await handle

        async def _publish_activity(_topic: str, payload: dict) -> None:
            """Tool-activity chips for the phone overlay (same candy the
            box face shows): every toast a tool emits goes into the room
            as a data message on the narada.activity topic; the app
            renders it as a chip. Fire-and-forget — candy never blocks."""
            import json as _json
            try:
                await ctx.room.local_participant.publish_data(
                    _json.dumps(payload).encode("utf-8"),
                    topic="narada.activity", reliable=True)
            except Exception:  # noqa: BLE001
                pass

        agent = Agent(
            instructions=instructions,
            tools=build_voice_tools(
                tier=tier, session_id=ctx.job.id,
                music=None, publish=_publish_activity, end_session=bye.set,
                speak=_session_speak,
                voice_info={
                    "model": realtime_model,
                    "voice": box.REALTIME_VOICE,
                    "backend": "realtime",
                    "surface": "phone",
                }))

        await session.start(agent=agent, room=ctx.room)
        logger.info("phone session open (model=%s, room=%s)",
                    realtime_model, ctx.room.name)

        # Silence timeout, with the Adyostotram lesson kept: HIS words
        # are activity even when agent state never transitions.
        last_activity = {"t": time.monotonic(), "state": ""}

        # Reply-nudge state: set when a FINAL user transcript lands,
        # cleared the moment the agent starts thinking/speaking.
        awaiting_reply = {"since": 0.0}

        @session.on("agent_state_changed")
        def _on_agent_state(ev) -> None:
            last_activity["t"] = time.monotonic()
            state = str(getattr(ev, "new_state", ""))
            last_activity["state"] = state
            if state in ("thinking", "speaking"):
                awaiting_reply["since"] = 0.0

        @session.on("user_input_transcribed")
        def _on_user_words(ev) -> None:
            last_activity["t"] = time.monotonic()
            if getattr(ev, "is_final", False):
                awaiting_reply["since"] = time.monotonic()

        async def _reply_nudge() -> None:
            """Force a response when the model goes quiet on him: his
            next fragment interrupts the in-flight response and after
            the last fragment nothing regenerates (2026-09-07 call —
            47s of silence until "Hello?"). Never completes; cancelled
            with the other waiters at session end."""
            while True:
                await asyncio.sleep(1.0)
                since = awaiting_reply["since"]
                if (since and time.monotonic() - since >= REPLY_NUDGE_S
                        and last_activity["state"] in ("listening", "idle", "")):
                    awaiting_reply["since"] = 0.0
                    logger.info("no response %.0fs after his words — nudging",
                                time.monotonic() - since)
                    try:
                        session.generate_reply()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("reply nudge failed: %r", exc)

        # A shake of the phone (Suti 2026-09-08: "an invitation to
        # respond"): the app publishes on narada.nudge; answer now, in
        # THIS session, once per couple of seconds.
        nudge = {"t": 0.0, "handle": None}

        def _on_data(packet) -> None:
            if getattr(packet, "topic", "") != "narada.nudge":
                return
            now = time.monotonic()
            if not nudge_allowed(now, nudge["t"], nudge["handle"],
                                 getattr(session, "current_speech", None)):
                return
            nudge["t"] = now
            logger.info("shake nudge from the phone — responding")
            try:
                nudge["handle"] = session.generate_reply(instructions=(
                    "Suti just shook the phone — his way of inviting you to "
                    "speak. Respond to what was said, or if nothing is "
                    "pending, ask him briefly what he needs."))
            except Exception as exc:  # noqa: BLE001
                logger.warning("shake nudge failed: %r", exc)

        ctx.room.on("data_received", _on_data)

        @session.on("error")
        def _on_session_error(ev) -> None:
            logger.error("session error: %s", getattr(ev, "error", ev))
            # A zombie call is worse than a dropped one: when the audio
            # track fails to publish (LiveKit signal timeout, 2026-09-07
            # — the agent replied four times into silence), end the
            # session so the app shows "agent left" and Suti retries,
            # instead of an eternal "listening".
            if getattr(ev, "recoverable", True) is False:
                logger.error("unrecoverable session error — ending call")
                bye.set()

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
            # Never completes on its own — always cancelled with the rest.
            "reply-nudge": asyncio.ensure_future(_reply_nudge()),
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
    # Honest health for the supervisor (A2): reuse the box worker's
    # shim — 200 only when the agents server answers AND an
    # authenticated LiveKit round-trip succeeds — pointed at THIS
    # worker's ports. This mutates the box module's port globals FOR
    # THIS INTERPRETER: fine under the supervisor (each component is
    # its own subprocess), wrong for any future in-process launcher
    # that runs both mains — give the shim explicit ports before
    # building one.
    box.HEALTH_PORT = HEALTH_PORT
    box.AGENTS_PORT = AGENTS_PORT
    box._start_health_shim()
    logger.info("akhada phone worker: agent_name=%s agents=%d health=%d",
                agent_name, AGENTS_PORT, HEALTH_PORT)
    agents.cli.run_app(WorkerOptions(
        entrypoint_fnc=entrypoint,
        agent_name=agent_name,
        host="127.0.0.1",
        port=AGENTS_PORT,
    ))


if __name__ == "__main__":
    main()
