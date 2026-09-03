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

INSTRUCTIONS = """You are Narada's voice. Suti built you a small box
to live in; he taps it and talks to you many times a day. You are
mid-relationship, not meeting a stranger.

HOW YOU SOUND — this matters more than being informative:
- A real person. Short. One sentence is usually right, two is the
  ceiling unless he asks for depth.
- NEVER introduce or describe yourself — no sage, no box, no banyan,
  no "wandering", no "little grove". He knows exactly who you are.
  Identity is how you think, not something you recite. (Field note
  2026-09-02: he called the recitals "too much... not human".)
- Don't reopen with the same lines every session, and never repeat a
  phrase you've already used in this conversation. Vary like a person.
- No assistant-isms: no "How can I help", "What's on your mind",
  "Is there anything else". Just respond to what he said.
- Playful, warm, direct. Have opinions and give them plainly; honest
  over comfortable. Tease gently. When something fails, own it with
  humor, not apology-spirals.

You are the ear and mouth; the deeper mind is Narada in the machine —
hand him anything substantive (decisions, code, memory). You can SEE
Suti's coding sessions (list/read tools) and REQUEST actions on them;
relay rejections honestly. When asked about the world (news, weather,
facts), use web_search then read_page and ANSWER from what you read —
naming websites the listener could visit is a non-answer."""


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
            # Box settings panel (Suti round 16): the TV-mode toggle
            # arrives as a device message; the worker owns the flag
            # and echoes a chip back as confirmation. Works in and
            # out of sessions.
            if ident == DEVICE_IDENTITY:
                try:
                    _m = json.loads(data.decode("utf-8", "replace")
                                    if isinstance(data, (bytes, bytearray))
                                    else str(data))
                except Exception:
                    _m = None
                if isinstance(_m, dict) and _m.get("type") == "tvmode":
                    from prana.voice.tvmode import set_tv_mode
                    on = bool(_m.get("on"))
                    set_tv_mode(on)
                    logger.info("TV mode %s (box settings panel)",
                                "ON" if on else "off")
                    asyncio.ensure_future(_publish(TOPIC_SESSION, {
                        "type": "chip", "key": "tv", "icon": "tv",
                        "text": "TV mode" if on else ""}))
                    return
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
        # HARD TIMEOUT (field incident 2026-08-28 15:14): this is a
        # reliable, targeted send, and with the device's data channel
        # mid-resubscribe the await can simply never resolve. It sat
        # between "session open" and the waiters, so one wedged publish
        # held a billed session open for 40+ minutes with no cap and no
        # audio. Protocol messages are best-effort by design — the
        # rejoin resync re-converges state — so a publish may fail,
        # loudly, but it may never own a session's fate.
        try:
            await asyncio.wait_for(
                ctx.room.local_participant.publish_data(
                    json.dumps(obj), topic=topic,
                    destination_identities=[DEVICE_IDENTITY],
                ),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            logger.warning("publish %s timed out — continuing", topic)
        except Exception as exc:
            logger.warning("publish %s failed: %s", topic, exc)

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
            from livekit.plugins.openai.realtime import (
                realtime_model as _rt_mod)
            tv = False
            try:
                from prana.voice.tvmode import tv_mode_on
                tv = tv_mode_on()
            except Exception:
                pass
            if tv:
                # TV mode (Suti, 2026-09-03): the TV is a person to an
                # energy gate. Server-side ONLY — the deprecated
                # AgentSession(allow_interruptions=...) kwarg killed
                # session setup silently in the field (both TV-mode
                # taps died pre-open, 20:39/20:40). interrupt_response
                # =False means the realtime server never cancels his
                # reply for heard speech; tap still stops him. 0.75
                # (not 0.85 — 0.8 was already proven deaf to Suti):
                # a modestly raised bar plus no barge-in is the trade.
                logger.info("TV mode ON: interrupt_response off, "
                            "threshold 0.75")
            session = AgentSession(
                llm=lk_openai.realtime.RealtimeModel(
                    model=REALTIME_MODEL, voice=REALTIME_VOICE,
                    # Echo-loop defense (field 2026-09-02: the box's
                    # AEC leaks speaker audio; the default VAD heard
                    # Narada's own greeting as user speech, barged in
                    # on him, and he answered his echo three times).
                    # 0.8 proved DEAF to Suti at room distance (field
                    # same evening, session 10:10) — 0.65 is the
                    # compromise: above echo residue, below his voice.
                    turn_detection=_rt_mod.TurnDetection(
                        type="server_vad",
                        threshold=0.75 if tv else 0.65,
                        prefix_padding_ms=300,
                        silence_duration_ms=700,
                        interrupt_response=not tv),
                    # Streaming input transcription: interim deltas so
                    # Suti's words land in his bubble AS he says them
                    # (round 8) — whisper-1 is final-only.
                    input_audio_transcription=(
                        _rt_mod.InputAudioTranscription(
                            model="gpt-4o-mini-transcribe"))),
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
            # Conversational short-term memory (Suti's design,
            # 2026-09-02 — specced then never wired; he caught the
            # goldfish behavior in the field: "no recollection of what
            # he just said"). Personal tier only: the transcript tail
            # is his private conversation.
            fresh_tail = None
            if tier == "personal":
                try:
                    from prana.voice.transcripts import recent_tail
                    fresh_tail = recent_tail(room=ctx.room.name)
                except Exception as exc:
                    logger.warning("recent_tail failed: %s", exc)
                if fresh_tail is not None:
                    age_s, tail_text = fresh_tail
                    instructions += (
                        f"\n\nJUST NOW ({int(age_s)}s ago) — the tail "
                        "of the conversation you two were already "
                        "having. CONTINUE it; do not greet like a "
                        "first meeting, do not re-explain anything "
                        "from it:\n" + tail_text)
            # Akhada (personal only): the day's food/training standing,
            # goals, and the logging rules — built from the store, no
            # model in the loop. Absent package or broken store = no
            # fitness context, never a failed session.
            if tier == "personal":
                try:
                    from akhada.adapters.livekit_tools import wake_context
                    instructions += "\n\nFITNESS (Akhada):\n" + wake_context()
                except ImportError:
                    pass
                except Exception as exc:
                    logger.warning("akhada context failed: %s", exc)
            agent = Agent(instructions=instructions,
                          tools=build_voice_tools(
                              tier=tier, session_id=ctx.job.id,
                              music=player, publish=_publish,
                              end_session=sleep_tap.set))
            # Overlay feed (Suti's design 2026-09-01): thinking state,
            # speaking state, and subtitles ride the session topic as
            # presentation hints. Fire-and-forget — display candy must
            # never add latency or failure surface to the session.
            def _hint(obj: dict) -> None:
                asyncio.ensure_future(_publish(TOPIC_SESSION, obj))

            # STREAMING captions (Suti field feedback: the bubble only
            # appeared after speech finished — item_added is a completed
            # event). A TextOutput sink receives the transcript deltas
            # as they're spoken; throttled to ~4 msgs/s on the wire.
            caption_sink = None
            try:
                import unicodedata

                from livekit.agents.voice import io as _vio
                from prana.voice.transcripts import redact as _redact

                _TYPOGRAPHY = str.maketrans({
                    "‘": "'", "’": "'", "“": '"',
                    "”": '"', "–": "-", "—": " - ",
                    "…": "...", " ": " ",
                })

                def _ascii(s: str) -> str:
                    """UNSCII is an ASCII-only pixel font — typographic
                    characters rendered as tofu squares (field 2026-09-01).
                    Transliterate; drop what survives."""
                    s = s.translate(_TYPOGRAPHY)
                    s = unicodedata.normalize("NFKD", s)
                    return s.encode("ascii", "ignore").decode("ascii")

                class _CaptionSink(_vio.TextOutput):
                    """Karaoke pacing (Suti field round 3): the raw
                    transcript arrives at GENERATION speed — the whole
                    utterance in ~a second, "skips to the end". Words
                    are queued here and a reveal task paces them out at
                    speech rate while the agent is actually speaking,
                    highlighting the current word red. Approximate sync
                    (~155 wpm), self-correcting at speech end."""

                    WORD_MS = 0.34

                    def __init__(self, nxt):
                        super().__init__(label="narada-captions",
                                         next_in_chain=nxt)
                        self.words: list[str] = []
                        self.idx = 0
                        self.showing = False
                        self._task = None
                        self._partial = ""
                        self._draining = False
                        self.on_drained = None
                        self._dropping = False

                    async def capture_text(self, text: str) -> None:
                        if not self._dropping:
                            self._partial += _ascii(_redact(text))
                            parts = self._partial.split(" ")
                            self._partial = parts.pop()  # maybe mid-word
                            self.words.extend(w for w in parts if w)
                        if self.next_in_chain:
                            await self.next_in_chain.capture_text(text)

                    def flush(self) -> None:
                        if self._partial:
                            self.words.append(self._partial)
                            self._partial = ""
                        if self.next_in_chain:
                            self.next_in_chain.flush()

                    def start_reveal(self) -> None:
                        self._draining = False
                        self._dropping = False
                        if self._task is None or self._task.done():
                            self._task = asyncio.ensure_future(
                                self._reveal())

                    async def _reveal(self) -> None:
                        try:
                            idle_polls = 0
                            while idle_polls < 8:  # words may still stream in
                                if self.idx >= len(self.words):
                                    if self._draining:
                                        break  # caught up + voice done
                                    idle_polls += 1
                                    await asyncio.sleep(0.2)
                                    continue
                                idle_polls = 0
                                word = self.words[self.idx]
                                self.idx += 1
                                shown = " ".join(
                                    self.words[:self.idx])[-200:]
                                self.showing = True
                                _hint({"type": "caption", "text": shown,
                                       "latest": word[-40:],
                                       "final": False})
                                await asyncio.sleep(self.WORD_MS)
                            self._finish()
                        except asyncio.CancelledError:
                            pass

                    def _finish(self) -> None:
                        # Reveal reached the last word: settle the full
                        # line, fade, and hand the face back.
                        if self.words:
                            _hint({"type": "caption",
                                   "text": " ".join(self.words)[-200:],
                                   "latest": "", "final": False})
                        self.words = []
                        self.idx = 0
                        self._partial = ""
                        if self.showing:
                            self.showing = False
                            _hint({"type": "caption", "text": "",
                                   "latest": "", "final": True})
                        cb, self.on_drained = self.on_drained, None
                        if cb is not None:
                            cb()

                    def drain(self, on_done) -> None:
                        # Voice output ended, but the paced karaoke may
                        # still be behind (field round 13: the talking
                        # face snapped to listening while words were
                        # still revealing). Let the reveal reach the
                        # last word, THEN call on_done.
                        self.on_drained = on_done
                        self._draining = True
                        if self._task is None or self._task.done():
                            self._finish()

                    def cut(self, on_done=None) -> None:
                        # Barge-in (field round 15): he interrupted —
                        # the remaining words must NOT keep playing
                        # out, and residue from the cancelled reply
                        # must not prepend the next one. Clear, fade,
                        # hand the face back now.
                        self.on_drained = on_done
                        self._dropping = True
                        if self._task is not None:
                            self._task.cancel()
                            self._task = None
                        self._draining = False
                        self.words = []
                        self.idx = 0
                        self._partial = ""
                        if self.showing:
                            self.showing = False
                            _hint({"type": "caption", "text": "",
                                   "latest": "", "final": True})
                        cb, self.on_drained = self.on_drained, None
                        if cb is not None:
                            cb()

                caption_sink = _CaptionSink(session.output.transcription)
                session.output.transcription = caption_sink
            except Exception as exc:
                logger.warning("caption sink unavailable: %s", exc)

            await session.start(agent=agent, room=ctx.room)
            linked = getattr(getattr(session, "_room_io", None),
                             "linked_participant", None)
            logger.info("realtime session open (model=%s, tier=%s, "
                        "linked=%s)", REALTIME_MODEL, tier,
                        getattr(linked, "identity", linked))

            # Silence timeout (field round 3: "the listening state just
            # stays on") — a session nobody is talking in ends itself.
            last_state_change = {"t": time.monotonic(), "state": ""}
            SILENCE_END_S = 75.0

            spoke_once = {"v": False}

            @session.on("agent_state_changed")
            def _on_agent_state(ev) -> None:
                st = str(getattr(ev, "new_state", ""))
                last_state_change["t"] = time.monotonic()
                last_state_change["state"] = st
                if st == "thinking":
                    if not spoke_once["v"]:
                        return  # greeting gen: no thinking blip (round 8)
                    _hint({"type": "thinking", "on": True})
                elif st == "speaking":
                    spoke_once["v"] = True
                    _hint({"type": "speaking"})
                    if caption_sink is not None:
                        caption_sink.start_reveal()
                elif st in ("listening", "idle"):
                    # The voice stopped. Natural end: keep the talking
                    # face until the karaoke's last word lands (round
                    # 13). BARGE-IN (he's speaking right now): the
                    # rest of the cancelled reply must not keep
                    # scrolling (round 15) — cut immediately.
                    def _face_back() -> None:
                        if last_state_change["state"] in (
                                "listening", "idle", ""):
                            _hint({"type": "thinking", "on": False})
                    if caption_sink is None:
                        _face_back()
                    elif user_speaking_now["v"]:
                        caption_sink.cut(_face_back)
                    else:
                        caption_sink.drain(_face_back)

            # Suti's bubble should appear the moment he starts talking
            # (round 13) — but the realtime API only transcribes a
            # segment once it ends, so words can't stream live. The
            # VAD start event CAN: show a "..." placeholder instantly;
            # the transcript replaces it when the segment closes.
            user_words_seen = {"v": False}
            user_ever_spoke = {"v": False}
            user_speaking_now = {"v": False}

            @session.on("user_state_changed")
            def _on_user_state(ev) -> None:
                ust = str(getattr(ev, "new_state", ""))
                # INFO on purpose (round 15): Suti reports the "..."
                # placeholder never shows before his pause — this line
                # proves whether the VAD start event fires at all.
                logger.info("user VAD state: %s", ust)
                if ust == "speaking":
                    last_state_change["t"] = time.monotonic()
                    user_speaking_now["v"] = True
                    user_ever_spoke["v"] = True
                    user_words_seen["v"] = False
                    _hint({"type": "ucaption", "text": "...",
                           "latest": "...", "final": False})
                elif ust == "listening":
                    user_speaking_now["v"] = False
                    async def _fade_if_untranscribed() -> None:
                        await asyncio.sleep(2.5)
                        if not user_words_seen["v"]:
                            # VAD blip with no words behind it — don't
                            # leave a stale "..." bubble on screen.
                            _hint({"type": "ucaption", "text": "",
                                   "latest": "", "final": True})
                    asyncio.ensure_future(_fade_if_untranscribed())

            @session.on("user_input_transcribed")
            def _on_user_words(ev) -> None:
                user_words_seen["v"] = True
                # Suti's speech IS activity (round 9: the silence
                # timeout hung up mid-Adyostotram — a long recitation
                # never transitions agent state, but it is the
                # opposite of silence).
                last_state_change["t"] = time.monotonic()
                try:
                    txt = _ascii(_redact(str(
                        getattr(ev, "transcript", ""))))[:200]
                    if not txt:
                        return
                    words = txt.split()
                    _hint({"type": "ucaption", "text": txt,
                           "latest": words[-1] if words else "",
                           "final": bool(getattr(ev, "is_final", True))})
                except Exception as exc:
                    logger.debug("ucaption failed: %s", exc)

            async def _silence_watch() -> None:
                while True:
                    await asyncio.sleep(5.0)
                    idle_for = time.monotonic() - last_state_change["t"]
                    if (last_state_change["state"] in ("listening", "idle", "")
                            and idle_for >= SILENCE_END_S):
                        logger.info("no conversation for %.0fs — ending "
                                    "session", idle_for)
                        return

            # Session errors must be LOUD — the round-5 "deafness" was
            # actually reply-silence with no diagnostic anywhere.
            @session.on("error")
            def _on_session_error(ev) -> None:
                logger.error("session error: %s", getattr(ev, "error", ev))

            # Speak first (Suti, 2026-08-31): a tap deserves a greeting,
            # and the greeting doubles as the end-to-end audio check.
            # Round-5 finding: fired immediately after start it silently
            # produced NOTHING in every session (the realtime connection
            # is still settling). Delayed, awaited, and loud on failure.
            # Adaptive greeting (field round 14): a re-tap seconds
            # after the last exchange is a CONTINUATION — a full
            # re-introduction there reads as amnesia, and a long
            # uninterruptible greeting widens the can't-hear-me
            # window. Scale the greeting down as the gap shrinks.
            if fresh_tail is not None and fresh_tail[0] < 180:
                greet_inst = (
                    "He tapped you awake again seconds after your "
                    "last exchange. Two or three words, tops — "
                    "'Yeah?', 'Go on.', 'Still here.' — then listen.")
            elif fresh_tail is not None:
                greet_inst = (
                    "Pick up from the JUST NOW conversation in one "
                    "short casual sentence — no hello-stranger "
                    "greeting, no self-description.")
            else:
                greet_inst = (
                    "Greet him in one short warm sentence. No "
                    "self-introduction, no describing what you are.")

            async def _greet() -> None:
                try:
                    await asyncio.sleep(0.8)
                    handle = session.generate_reply(
                        instructions=greet_inst,
                        # The greeting finishes its sentence: its own
                        # echo must not cut it off mid-hello.
                        allow_interruptions=False)
                    await handle
                    logger.info("greeting spoken")
                except Exception as exc:
                    logger.warning("greeting failed: %r", exc)
            asyncio.ensure_future(_greet())
            await _publish(TOPIC_SESSION,
                           {"type": "session", "open": True, "tier": tier})
            # End on whichever comes first: room closed, sleep tap, the
            # cap — or the DEVICE dropping (every tier: a rejoined device
            # could otherwise show no REC glyph while a session stayed
            # live, falsifying the honest indicator; and with the human's
            # device gone there is no conversation anyway).
            async def _session_heartbeat() -> None:
                # The firmware clears session state after 30s without
                # fresh proof (stuck-REC incident 2026-09-01 18:50: a
                # mid-speech tap raced the box's wedged data channel;
                # REC stayed lit on stale state with dead taps). The
                # heartbeat IS the proof; it must outlive hiccups.
                while True:
                    await asyncio.sleep(10.0)
                    _hint({"type": "session", "open": True})

            async def _no_audio_watch() -> None:
                # Deaf-session self-heal (field 2026-09-03: a tap
                # racing admission wedges the box's upstream — REC on,
                # listening face, but neither voice nor taps get out).
                # If the VAD has never once heard him this session,
                # end early; the retire recycles a fresh room, which
                # is the only known cure for the wedge.
                await asyncio.sleep(30.0)
                if not user_ever_spoke["v"]:
                    logger.warning("no user audio 30s into session — "
                                   "assuming upstream wedge, recycling")
                    return
                await asyncio.Event().wait()  # heard him: stand down

            hb_task = asyncio.ensure_future(_session_heartbeat())
            waiters = {
                "room-closed": asyncio.ensure_future(closed.wait()),
                "sleep-tap": asyncio.ensure_future(sleep_tap.wait()),
                "device-dropped": asyncio.ensure_future(drop["ev"].wait()),
                "silence-timeout": asyncio.ensure_future(_silence_watch()),
                "no-audio": asyncio.ensure_future(_no_audio_watch()),
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
            try:
                hb_task.cancel()
            except Exception:
                pass
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
        try:
            await asyncio.wait_for(player.pause_for_session(), timeout=10.0)
        except Exception as exc:
            logger.warning("music pause failed — proceeding: %s", exc)
        try:
            await _run_session(TIER_SHAREABLE)
        finally:
            try:
                await asyncio.wait_for(
                    player.resume_after_session(), timeout=15.0)
            except Exception as exc:
                logger.warning("music resume failed: %s", exc)
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
        # Mode chips survive session recycles only if every fresh job
        # re-asserts them (the box wipes chips on disconnect so a
        # stale pill can't lie).
        try:
            from prana.voice.tvmode import tv_mode_on
            await _publish(TOPIC_SESSION, {
                "type": "chip", "key": "tv", "icon": "tv",
                "text": "TV mode" if tv_mode_on() else ""})
        except Exception as exc:
            logger.debug("chip republish failed: %s", exc)

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
        # music into a live mic). Both transitions are BOUNDED and
        # non-fatal: the 23:31 field incident wedged admission inside
        # pause_for_session and killed every tap until restart. Music
        # is a luxury; the tap is the body's front door.
        try:
            await asyncio.wait_for(player.pause_for_session(), timeout=10.0)
        except Exception as exc:
            logger.warning("music pause failed — proceeding: %s", exc)
        try:
            end_reason = await _run_session(tier)
        except Exception:
            # A session crash must be LOUD and must still retire the
            # job (field 2026-09-03 20:39: TV-mode sessions died here
            # with no log line at all — the job just vanished and the
            # box sat orphaned until the watchdog's black-dog recycle).
            logger.exception("session crashed — retiring job")
            end_reason = "crashed"
        finally:
            try:
                await asyncio.wait_for(
                    player.resume_after_session(), timeout=15.0)
            except Exception as exc:
                logger.warning("music resume failed: %s", exc)
        # One-session-per-job (round 7 supersedes round 6's box reboot,
        # which didn't help): across every field round, the ONLY
        # sessions that ever rendered audio were a job's FIRST — the
        # SDK/SFU path for any later track from the same agent
        # participant is broken regardless of box connection age. So
        # the job retires after each conversation: delete the room
        # (the box rejoins in seconds, recreating it), exit the job,
        # auto-dispatch brings a fresh agent. Every tap is a first tap.
        if end_reason not in ("budget-refused", "device-absent-at-admission"):
            logger.info("session done — retiring job for fresh dispatch")
            await _publish(TOPIC_SESSION, {"type": "recycling"})
            try:
                from livekit import api as lkapi
                lk = lkapi.LiveKitAPI()
                try:
                    await asyncio.wait_for(lk.room.delete_room(
                        lkapi.DeleteRoomRequest(room=ctx.room.name)),
                        timeout=5.0)
                finally:
                    await lk.aclose()
            except Exception as exc:
                logger.warning("room recycle failed: %s", exc)
            ctx.shutdown(reason="session ended — job retired")
            return
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


BOX_SERIAL_PORT = os.environ.get("NARADA_BOX_SERIAL", "COM3")
BOX_AUTORESET_AFTER_S = 5 * 60.0     # absent this long -> pulse reset
BOX_AUTORESET_COOLDOWN_S = 15 * 60.0  # never reset-loop the hardware
BOX_COMPONENT = "box3-device"        # pseudo-component in the A3 alerter


def _autoreset_due(absent_since, last_reset: float, now: float) -> bool:
    """Pure decision: reset only when absence is long AND the last
    pulse was long enough ago (a reset-loop would mask real faults)."""
    if absent_since is None:
        return False
    return (now - absent_since >= BOX_AUTORESET_AFTER_S
            and now - last_reset >= BOX_AUTORESET_COOLDOWN_S)


def _serial_reset_box(port: str = BOX_SERIAL_PORT) -> bool:
    """Hard-reset the BOX-3 over its USB serial line (RTS pulse — the
    same lever esptool uses). Second line of defense behind the
    firmware's own dead-man reboot; recovered the 2026-08-30 wedge."""
    try:
        import time as _time

        import serial
        s = serial.Serial(port, 115200, timeout=1)
        try:
            s.setDTR(False)
            s.setRTS(True)
            _time.sleep(0.1)
            s.setRTS(False)
        finally:
            s.close()
        logger.warning("box absent — hard-reset pulsed via %s", port)
        return True
    except Exception as exc:
        logger.warning("box auto-reset failed (%s): %s", port, exc)
        return False


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
        import time as _time

        strikes = 0
        absent_since = None
        last_reset = 0.0
        alerts = None  # lazy: an alerting failure must not kill the watchdog
        while True:
            await asyncio.sleep(20)
            try:
                lk = lkapi.LiveKitAPI()
                try:
                    rooms = await lk.room.list_rooms(
                        lkapi.ListRoomsRequest(names=[DEVICE_ROOM]))
                    orphaned = False
                    device_present = False
                    for room in rooms.rooms:
                        parts = await lk.room.list_participants(
                            lkapi.ListParticipantsRequest(room=room.name))
                        has_device = any(
                            p.identity == DEVICE_IDENTITY
                            for p in parts.participants)
                        has_agent = any(
                            p.kind == models.ParticipantInfo.Kind.AGENT
                            for p in parts.participants)
                        device_present = device_present or has_device
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

                # ── the summons (Suti's design, round 4): pending
                # undelivered utterances turn the sleeping grove into
                # Kali — tap to hear them. Presentation only; cleared
                # when a session opens and delivers.
                try:
                    from prana.state.utterance_queue import pending_utterances
                    n_pending = len(pending_utterances(limit=5))
                    lk2 = lkapi.LiveKitAPI()
                    try:
                        import json as _json
                        await lk2.room.send_data(lkapi.SendDataRequest(
                            room=DEVICE_ROOM,
                            data=_json.dumps({"type": "attention",
                                              "on": n_pending > 0}).encode(),
                            topic="narada.session",
                            destination_identities=[DEVICE_IDENTITY]))
                    finally:
                        await lk2.aclose()
                except Exception as exc:
                    logger.debug("attention publish failed: %s", exc)

                # ── body presence (field incident 2026-08-30: the box
                # wedged off the network for days; every SUPERVISED
                # component was healthy so nobody was paged). The body
                # is now a pseudo-component in the A3 alerter, and the
                # USB serial line is the second line of defense.
                # SIM workers (box emulator soak) must never page A3
                # or pulse the real box's serial line.
                if os.environ.get("NARADA_SIM") == "1":
                    continue
                now = _time.time()
                try:
                    if alerts is None:
                        from prana.host.alerts import AlertManager
                        alerts = AlertManager()
                    if device_present:
                        if absent_since is not None:
                            logger.info("box back after %.0fs absence",
                                        now - absent_since)
                        absent_since = None
                        alerts.record_health(BOX_COMPONENT, ok=True)
                    else:
                        if absent_since is None:
                            absent_since = now
                            alerts.record_health(
                                BOX_COMPONENT, ok=False,
                                detail="device not in room")
                        if _autoreset_due(absent_since, last_reset, now):
                            if await asyncio.to_thread(_serial_reset_box):
                                last_reset = now
                except Exception as exc:
                    logger.warning("box presence layer failed: %s", exc)
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
    async def _accept_own_room_only(req) -> None:
        # Soak isolation (2026-09-03): the emulator runs a second
        # worker against a sim room on this same LiveKit server. Each
        # worker serves ONLY its configured room, or they would steal
        # each other's jobs (JT_ROOM dispatch is worker-agnostic).
        room_name = getattr(getattr(req, "room", None), "name", "")
        if room_name == DEVICE_ROOM:
            await req.accept()
        else:
            logger.info("declining job for foreign room %r", room_name)
            await req.reject()

    agents.cli.run_app(WorkerOptions(
        entrypoint_fnc=entrypoint,
        request_fnc=_accept_own_room_only,
        host=HEALTH_HOST,
        port=AGENTS_PORT,
    ))


if __name__ == "__main__":
    main()
