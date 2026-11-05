"""Gemini Live talker: speech-to-speech, budget-guarded (presence plan W5a).

Stage 1 of the talker/sage split. The phone worker gives the talker Narada's
own instructions and the tier-filtered voice tools (prana.voice.pipeline); the
voice lab uses it bare, with its test brief. Keys come from the environment
(`~/.narada/.keys.env`); they are never logged or sent to the browser.

Moved here from experiments/voice-lab on 2026-11-05 (the lab's
`gemini_adapter` is now an alias of this module). Settings read
NARADA_GEMINI_<NAME>, then the lab's VOICE_LAB_GEMINI_<NAME>.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from google.genai import types
from livekit import rtc
from livekit.agents import Agent, AgentSession
from livekit.plugins.google.realtime import realtime_api

from prana.voice.gemini.core import (
    DEFAULT_MODEL, DEFAULT_VOICE, Budget, CostGuard, IdleWatchdog, LatencyMeter, Ledger,
    LedgerError, NullSage, Pot, Sage, Transcript, estimate_tokens, instructions, pcm16_rms,
    month_start, rates_for, trial_ledger_path,
)


def _env(name: str, default: str) -> str:
    return os.getenv(f"NARADA_GEMINI_{name}") or os.getenv(f"VOICE_LAB_GEMINI_{name}") or default


# Per-call summaries and the pre-rendered closing clip. The lab points this at
# its own output/ (its alias sets NARADA_GEMINI_OUTPUT).
OUTPUT = Path(_env("OUTPUT", str(Path.home() / ".narada" / "voice" / "talker")))
# One trial ledger for every checkout and process (Codex review, 2026-11-04:
# a ledger in each checkout's output/ gave each its own full budget).
LEDGER_PATH = trial_ledger_path()
# Spend this process could not write to the ledger even at close (Codex
# review): the next call must record it before it may spend anything.
_UNRECORDED = {"usd": 0.0}
# Writes during a live call run on the event loop, so they give up fast:
# a busy or stale lock carries the charge to the close and stops the call,
# instead of freezing audio for the default 10 s (Codex review).
IN_CALL_LOCK_TIMEOUT_S = 0.5
# Before 2026-11-04 each checkout kept its own ledger here. None ever held
# spend (no paid run had happened), but if one appears, its balance must be
# reconciled into the shared ledger by hand before anything is spent.
LEGACY_LEDGER = OUTPUT / "gemini-ledger.json"
MODEL = _env("MODEL", DEFAULT_MODEL)
VOICE = _env("VOICE", DEFAULT_VOICE)
# Suti's caps (2026-11-05): $0.50 a session, US$10 a calendar month, across
# every Gemini caller (the worker, the lab, the smoke). The month replaced the
# interim "trials <= $5" total.
SESSION_CAP_USD = float(_env("SESSION_CAP_USD", "0.50"))
MONTH_CAP_USD = float(_env("MONTH_CAP_USD", "10.00"))
IDLE_SECONDS = float(_env("IDLE_SECONDS", "45"))
# Below Gemini's ~10-minute connection lifetime, so no GoAway reconnects.
MAX_SECONDS = min(540.0, float(_env("MAX_SECONDS", "300")))
# Bounds each turn's in-flight output: 500 audio tokens is 20 s of speech, <= $0.006.
MAX_OUTPUT_TOKENS = 500
# Bounds the context billed per turn on longer calls (sliding window).
COMPRESSION_TRIGGER_TOKENS, COMPRESSION_TARGET_TOKENS = 12_000, 6_000
CLOSING_TEXT = "I'll have to stop us there; that's today's limit. Talk soon."


def api_key() -> str | None:
    return os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")


def closing_clip_path(voice: str = VOICE) -> Path:
    """Pre-rendered 24 kHz mono s16le closing line; playing it costs nothing."""
    return OUTPUT / f"closing-{voice}.pcm"


class BudgetRefused(RuntimeError):
    pass


class MeteredSession(realtime_api.RealtimeSession):
    """The plugin session with three hooks: gate audio, observe content, reconcile usage.

    The two private hooks are pinned to livekit-plugins-google 1.8.10; a unit
    test fails if the plugin renames them.
    """

    talker: "GeminiTalker"

    def push_audio(self, frame: rtc.AudioFrame) -> None:
        if self.talker.admit_input(frame):
            super().push_audio(frame)

    def _handle_server_content(self, server_content: types.LiveServerContent) -> None:
        gen = self._current_generation
        super()._handle_server_content(server_content)
        self.talker.on_server_content(gen.response_id if gen else None, server_content)

    def _handle_usage_metadata(self, usage_metadata: types.UsageMetadata) -> None:
        # Billed even when the plugin has no generation to attach metrics to.
        gen = self._current_generation
        self.talker.on_usage(gen.response_id if gen else None,
                             usage_metadata.model_dump(mode="json", exclude_none=True))
        super()._handle_usage_metadata(usage_metadata)


class MeteredModel(realtime_api.RealtimeModel):
    def __init__(self, talker: "GeminiTalker", **kwargs):
        super().__init__(**kwargs)
        self._talker = talker

    def session(self, *, turn_detection_disabled: bool = False) -> MeteredSession:
        if self._talker.guard.stopped:
            raise BudgetRefused("Budget refused: no new provider session")
        # The connect task only starts on the next loop turn, after `talker` is set.
        sess = MeteredSession(self)
        sess.talker = self._talker
        self._sessions.add(sess)
        self._talker.rt = sess
        return sess


class GeminiTalker:
    """One call's talker: guard, idle timeout, transcript, latency, sage seam."""

    def __init__(self, call, *, session_cap_usd: float = SESSION_CAP_USD,
                 idle_seconds: float = IDLE_SECONDS, max_seconds: float = MAX_SECONDS,
                 sage: Sage | None = None, instructions_text: str | None = None,
                 voice: str | None = None):
        self.call = call
        self.model_name = MODEL
        self.voice = voice or VOICE
        self.rates = rates_for(MODEL)
        # The worker's full instructions (identity, memory pack, briefing), or
        # the lab's test brief; either way the guard prices the real context.
        self.instructions = instructions_text if instructions_text is not None else instructions()
        self.max_seconds = min(max_seconds, MAX_SECONDS)
        self.ledger = Ledger(LEDGER_PATH, lock_timeout=IN_CALL_LOCK_TIMEOUT_S)
        self.unrecorded_usd = 0.0
        if LEGACY_LEDGER.exists():
            raise BudgetRefused(f"Budget refused: reconcile the old per-checkout ledger "
                                f"{LEGACY_LEDGER} into {LEDGER_PATH}, then remove it")
        try:
            if _UNRECORDED["usd"] > 0:  # an earlier call's spend comes first
                self.ledger.add(call.id, _UNRECORDED["usd"], "late")
                _UNRECORDED["usd"] = 0.0
            self.since = month_start()
            month_spent = self.ledger.spent(self.since)
        except LedgerError as exc:  # never read an untrustworthy total as $0
            raise BudgetRefused(f"Budget refused: {exc}") from None
        self.month = Pot("month", MONTH_CAP_USD, spent_usd=month_spent)
        self.budget = Budget([Pot("session", min(session_cap_usd, SESSION_CAP_USD)), self.month])
        self.guard = CostGuard(self.budget, self.rates, max_output_tokens=MAX_OUTPUT_TOKENS,
                               base_context_tokens=estimate_tokens(self.instructions),
                               on_stop=self._on_stop)
        self.idle = IdleWatchdog(min(idle_seconds, IDLE_SECONDS))
        self.sage = sage or NullSage()
        self.transcript = Transcript(sink=self._on_transcript)
        self.latency = LatencyMeter()
        self.rt: MeteredSession | None = None
        self.live = False
        self.speaking = False   # replies arrive faster than they play; playback is activity
        self.tasks: set[asyncio.Task] = set()
        self.reserved_at_close = 0.0

    # -- set-up ---------------------------------------------------------------

    def model(self) -> MeteredModel:
        key = api_key()
        if not key:
            raise RuntimeError("Set GOOGLE_API_KEY (or GEMINI_API_KEY) in ~/.narada/.keys.env")
        return MeteredModel(
            self, model=MODEL, api_key=key, voice=self.voice,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            # 3.8 Live: proactive audio is always on and affective dialog was removed,
            # so neither option is sent (setting proactive_audio=false is an error).
            context_window_compression=types.ContextWindowCompressionConfig(
                trigger_tokens=COMPRESSION_TRIGGER_TOKENS,
                sliding_window=types.SlidingWindow(target_tokens=COMPRESSION_TARGET_TOKENS)),
        )

    def open(self) -> None:
        """Reserve the first turn before any paid connection is made."""
        if not self.guard.open():
            raise BudgetRefused(f"Budget refused before connecting: {self.budget.snapshot()}")
        self.call.emit("budget_opened", budget=self.budget.snapshot(),
                       first_turn_ceiling_usd=round(self.guard.turn_ceiling(), 6))

    def attach(self, session: AgentSession) -> None:
        @session.on("conversation_item_added")
        def item(ev):
            role = getattr(ev.item, "role", None)
            text = getattr(ev.item, "text_content", "") or ""
            if role == "user" and text:
                self.transcript.user(text)
            elif role == "assistant":
                self.transcript.played(getattr(ev.item, "id", None), text,
                                       interrupted=bool(getattr(ev.item, "interrupted", False)))

        @session.on("agent_state_changed")
        def agent_state(ev):
            self.speaking = ev.new_state == "speaking"
            self.idle.touch()

        @session.on("user_state_changed")
        def user_state(ev):
            if ev.new_state == "speaking":
                self.idle.touch()

        self.live = True
        self.idle.touch()
        self._spawn(self._watch_idle())

    # -- the three provider hooks --------------------------------------------

    def admit_input(self, frame: rtc.AudioFrame) -> bool:
        rms = pcm16_rms(frame.data)
        self.latency.on_input(rms)
        if rms >= self.latency.threshold:
            self.idle.touch()
        return self.guard.admit_audio(frame.samples_per_channel / frame.sample_rate)

    def on_server_content(self, gen_id: str | None, sc: types.LiveServerContent) -> None:
        # The first sign of a reply (its audio or its transcript) reserves
        # its turn ceiling, whether or not a usage report ever follows.
        if gen_id and (sc.output_transcription or (sc.model_turn and sc.model_turn.parts)):
            self.guard.on_generation(gen_id)
        if gen_id and sc.output_transcription and sc.output_transcription.text:
            self.transcript.generated(gen_id, sc.output_transcription.text)
            self.idle.touch()
        if sc.input_transcription and sc.input_transcription.text:
            self.idle.touch()
        if gen_id and sc.model_turn and any(p.inline_data for p in sc.model_turn.parts or []):
            self.idle.touch()
            sample = self.latency.on_model_audio(gen_id)
            if sample:
                self.call.emit("latency", **sample)
        if sc.interrupted:
            self.call.emit("provider_interrupted", gen_id=gen_id)

    def on_usage(self, gen_id: str | None, usage: dict) -> None:
        usd = self.guard.on_usage(usage, gen_id)
        if self._record(usd, "usage"):
            # The month's total now includes any other process's spend; the
            # next turn was reserved before that was known.
            self.guard.recheck()
        self.call.emit("provider_usage", api="gemini-live", gen_id=gen_id, usage=usage,
                       usd=round(usd, 6), budget=self.budget.snapshot())

    def _record(self, usd: float, kind: str) -> bool:
        """Write spend to the shared ledger now, and take the month's total from
        it (another process's spend included). A ledger that can't be written
        stops the call: no unrecorded spending."""
        if usd <= 0:
            return True
        try:
            self.month.spent_usd = self.ledger.add(self.call.id, usd, kind, since=self.since)
            return True
        except (LedgerError, OSError) as exc:
            # Kept, not dropped: close retries it (Codex review).
            self.unrecorded_usd += usd
            # `entry`, not `kind`: emit's own first argument is the kind.
            self.call.emit("ledger_write_failed", entry=kind, usd=round(usd, 6),
                           error=type(exc).__name__)
            self.guard.stop("ledger")
            return False

    # -- the sage seam (stage 1: observe only) ---------------------------------

    def _on_transcript(self, event: dict) -> None:
        # The lab's event log owns "seq"; the transcript's own index travels as "turn".
        self.call.emit(event["kind"], **{("turn" if k == "seq" else k): v
                                         for k, v in event.items() if k != "kind"})
        self.sage.observe(event)

    def inject_context(self, text: str) -> bool:
        """Stage 2 hook: the sage whispers context to the talker. A no-op today.

        On gemini-3.8-live the route is a mid-session client-content turn
        (turn_complete=False), which the 3.8 model accepts; 3.1 did not.
        """
        self.call.emit("sage_inject_ignored", stage=1, chars=len(text))
        return False

    # -- stopping ---------------------------------------------------------------

    def _on_stop(self, reason: str) -> None:
        # The guard already refuses audio. Close on the next loop tick, so the
        # usage report that triggered the stop is logged before cap_reached.
        if self.live:
            asyncio.get_running_loop().call_soon(self._cap_reached, reason)

    def _cap_reached(self, reason: str) -> None:
        self.call.emit("cap_reached", reason=reason, budget=self.budget.snapshot(),
                       audio_seconds_refused=round(self.guard.audio_seconds_refused, 3))
        self._spawn(self.call.close(reason="cap"))

    async def _watch_idle(self) -> None:
        while not self.call.closed:
            await asyncio.sleep(max(0.2, min(1.0, self.idle.remaining())))
            if self.speaking:
                self.idle.touch()
            if self.idle.expired() and not self.call.closed:
                self.call.emit("idle_timeout", seconds=self.idle.timeout)
                await self.call.close(reason="idle")
                return

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def finish(self, room: rtc.Room, reason: str) -> None:
        """After the provider session is closed: account, play the free closing line, write the transcript."""
        self.reserved_at_close = sum(r.usd for r in ([self.guard.turn] if self.guard.turn else [])
                                     + list(self.guard.started.values()) + self.guard.audio)
        # Usage was recorded report by report; this adds only what was
        # billed without a report (a reply cut off by the close).
        charged = self.guard.close()
        # Retry anything a failed write left over, together with the close.
        owed, self.unrecorded_usd = self.unrecorded_usd + charged, 0.0
        if not self._record(owed, "close"):
            _UNRECORDED["usd"] += self.unrecorded_usd   # the next call records it first
            self.unrecorded_usd = 0.0
        self.transcript.finish()
        if reason == "cap":
            await self._play_closing(room)
        OUTPUT.mkdir(parents=True, exist_ok=True)
        path = OUTPUT / f"talker-{self.call.id}.transcript.json"
        summary = {
            "stage": 1, "call_id": self.call.id, "model": self.model_name, "voice": self.voice,
            "reason": reason, "cost_usd": round(self.guard.actual_usd, 6),
            "charged_unreported_usd": round(self.guard.charged_at_close, 6),
            "charged_unreported_replies_usd": round(self.guard.charged_replies_at_close, 6),
            "charged_trailing_audio_usd": round(self.guard.charged_audio_at_close, 6),
            "unrecorded_usd": round(_UNRECORDED["usd"], 6),
            "replies_started": len(self.guard.seen),
            "turn_ceiling_usd": round(self.guard.turn_ceiling(), 6),
            "unreconciled_ceiling_usd": round(self.reserved_at_close, 6),
            "usage_reports": self.guard.reports, "budget": self.budget.snapshot(),
            "audio_seconds_admitted": round(self.guard.audio_seconds_admitted, 3),
            "audio_seconds_refused": round(self.guard.audio_seconds_refused, 3),
            "latency": self.latency.samples,
        }
        path.write_text(json.dumps({**summary, "heard": self.transcript.heard(),
                                    "entries": self.transcript.entries()}, indent=2,
                                   ensure_ascii=False), encoding="utf-8")
        self.call.emit("talker_summary", transcript=path.name, **summary)

    async def _play_closing(self, room: rtc.Room) -> None:
        clip = closing_clip_path(self.voice)
        if not clip.is_file():
            self.call.emit("closing_clip_missing", path=clip.name)
            return
        pcm = clip.read_bytes()
        source = rtc.AudioSource(24000, 1, queue_size_ms=200)
        track = rtc.LocalAudioTrack.create_audio_track("talker-closing-line", source)
        try:
            pub = await room.local_participant.publish_track(
                track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
            for offset in range(0, len(pcm), 960):
                chunk = pcm[offset:offset + 960].ljust(960, b"\0")
                await source.capture_frame(rtc.AudioFrame(chunk, 24000, 1, 480))
            await asyncio.wait_for(source.wait_for_playout(), 10)
            await room.local_participant.unpublish_track(pub.sid)
            self.call.emit("closing_clip_played", text=CLOSING_TEXT, usd=0)
        except Exception as exc:  # the call is ending either way
            self.call.emit("closing_clip_error", error_type=type(exc).__name__)
        finally:
            await source.aclose()


def open_talker(call, *, instructions_text: str | None = None, voice: str | None = None,
                session_cap_usd=SESSION_CAP_USD, idle_seconds=IDLE_SECONDS,
                max_seconds=MAX_SECONDS) -> tuple[GeminiTalker, MeteredModel]:
    """A guarded talker and its model, with the first turn already reserved.
    Raises BudgetRefused (spend nothing) or RuntimeError (no key)."""
    talker = GeminiTalker(call, session_cap_usd=session_cap_usd, idle_seconds=idle_seconds,
                          max_seconds=max_seconds, instructions_text=instructions_text,
                          voice=voice)
    model = talker.model()
    talker.open()
    return talker, model


def make_gemini(call, *, session_cap_usd=SESSION_CAP_USD, idle_seconds=IDLE_SECONDS,
                max_seconds=MAX_SECONDS):
    """The lab's bare talker: the test brief, no tools."""
    talker, model = open_talker(call, session_cap_usd=session_cap_usd,
                                idle_seconds=idle_seconds, max_seconds=max_seconds)
    return talker, model, Agent(instructions=talker.instructions)
