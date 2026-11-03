"""Gemini Live talker core: prices, budget, guards, transcript and latency.

Pure Python with no LiveKit or Google imports, so every rule here is unit-tested
without a network. `gemini_adapter.py` wires it into the plugin.

Stage 1 of the talker/sage plan (narada-presence-2026-11-03, W5): the talker
speaks alone and the sage reads the transcript after the call. The `Sage`
protocol and `inject_context` below are the seam for later stages; today they
observe and do nothing.
"""
from __future__ import annotations

from array import array
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import time
from typing import Callable, Protocol

# --- Model, voice and prices -------------------------------------------------

# Stable since September 2026; replaces the legacy gemini-3.1-flash-live-preview.
# https://aistudio.google.com/docs/models/gemini-3.8-live
DEFAULT_MODEL = "gemini-3.8-live"
# Male, "Easy-going" in Google's voice table. Alternates: Algieba (Smooth),
# Achird (Friendly). A descriptor-based pick; Suti's ear decides.
DEFAULT_VOICE = "Umbriel"
AUDIO_TOKENS_PER_SECOND = 25


@dataclass(frozen=True)
class Rates:
    """Paid-tier USD per million tokens (https://aistudio.google.com/docs/pricing, 2026-11-03)."""
    text_in: float
    audio_in: float
    text_out: float
    audio_out: float

    @property
    def max_in(self) -> float:
        return max(self.text_in, self.audio_in)

    @property
    def max_out(self) -> float:
        return max(self.text_out, self.audio_out)


_LIVE_3 = Rates(text_in=0.75, audio_in=3.00, text_out=4.50, audio_out=12.00)
RATES = {
    "gemini-3.8-live": _LIVE_3,
    "gemini-3.8-live-extended-thinking": _LIVE_3,
    "gemini-3.1-flash-live-preview": _LIVE_3,
    "gemini-2.5-flash-native-audio-preview-12-2025": Rates(0.50, 3.00, 2.00, 12.00),
}


def rates_for(model: str) -> Rates:
    try:
        return RATES[model.removeprefix("models/")]
    except KeyError:
        raise ValueError(f"No checked price for {model!r}; add it to RATES before spending") from None


def _modality(detail: dict) -> str:
    return str(detail.get("modality", "")).rsplit(".", 1)[-1].upper()


def _priced(total: int, details: list[dict] | None, *, text: float, audio: float) -> float:
    """Price `total` tokens by modality; anything unlabelled pays the higher rate."""
    usd = 0.0
    counted = 0
    for d in details or []:
        n = int(d.get("token_count") or 0)
        counted += n
        m = _modality(d)
        usd += n * (text if m == "TEXT" else audio if m in ("AUDIO", "VIDEO", "IMAGE") else max(text, audio))
    usd += max(0, total - counted) * max(text, audio)
    return usd / 1e6


def price_usage(usage: dict, rates: Rates) -> float:
    """USD for one Live `usage_metadata` report (JSON-mode dump).

    Conservative by design: the 3.8 Live model has no context caching, so
    cached tokens are not discounted; unlabelled tokens pay the higher rate;
    thinking tokens pay the text output rate.
    """
    usd = _priced(int(usage.get("prompt_token_count") or 0), usage.get("prompt_tokens_details"),
                  text=rates.text_in, audio=rates.audio_in)
    usd += _priced(int(usage.get("tool_use_prompt_token_count") or 0),
                   usage.get("tool_use_prompt_tokens_details"), text=rates.text_in, audio=rates.audio_in)
    usd += _priced(int(usage.get("response_token_count") or 0), usage.get("response_tokens_details"),
                   text=rates.text_out, audio=rates.audio_out)
    usd += int(usage.get("thoughts_token_count") or 0) * rates.text_out / 1e6
    return usd


def estimate_tokens(text: str) -> int:
    """Rough upper estimate for English text (about 4 characters a token, rounded up)."""
    return math.ceil(len(text) / 3.5)


# --- Budget ------------------------------------------------------------------

@dataclass
class Pot:
    name: str
    cap_usd: float
    spent_usd: float = 0.0
    reserved_usd: float = 0.0

    @property
    def headroom(self) -> float:
        return self.cap_usd - self.spent_usd - self.reserved_usd


@dataclass
class Reservation:
    usd: float
    label: str
    open: bool = True


class Budget:
    """Reserve-then-settle across every pot at once (session cap, trial total).

    A reservation is granted only if every pot has headroom for it. Settling
    releases the reservation and charges the actual cost, which may exceed it;
    actual spend is always recorded, never refused.
    """

    def __init__(self, pots: list[Pot]):
        self.pots = pots

    def reserve(self, usd: float, label: str) -> Reservation | None:
        usd = max(0.0, usd)
        if any(p.headroom < usd - 1e-12 for p in self.pots):
            return None
        for p in self.pots:
            p.reserved_usd += usd
        return Reservation(usd, label)

    def settle(self, reservation: Reservation, actual_usd: float = 0.0) -> None:
        if reservation.open:
            reservation.open = False
            for p in self.pots:
                p.reserved_usd = max(0.0, p.reserved_usd - reservation.usd)
        self.charge(actual_usd)

    def charge(self, usd: float) -> None:
        for p in self.pots:
            p.spent_usd += max(0.0, usd)

    @property
    def exhausted(self) -> bool:
        return any(p.spent_usd >= p.cap_usd for p in self.pots)

    def snapshot(self) -> dict:
        return {p.name: {"cap_usd": p.cap_usd, "spent_usd": round(p.spent_usd, 6),
                         "reserved_usd": round(p.reserved_usd, 6)} for p in self.pots}


class Ledger:
    """Trial-total spend across lab sessions (the plan's interim "trials total <= $5")."""

    def __init__(self, path: Path):
        self.path = path

    def spent(self) -> float:
        try:
            return float(json.loads(self.path.read_text(encoding="utf-8"))["spent_usd"])
        except (FileNotFoundError, KeyError, ValueError):
            return 0.0

    def add(self, call_id: str, usd: float) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            data = {"spent_usd": 0.0, "sessions": []}
        data["spent_usd"] = round(data["spent_usd"] + usd, 6)
        data["sessions"].append({"call_id": call_id, "usd": round(usd, 6), "at": time.time()})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# --- Cost guard: reserve before every paid operation --------------------------

class CostGuard:
    """Speech-to-speech enforcement of the per-session and trial caps.

    The paid operations we can gate are (1) opening the provider session,
    (2) each chunk of microphone audio sent to it, and (3) the next model
    turn, whose cost we bound by its context and `max_output_tokens`.

    - `open()` reserves the first turn's ceiling before any connection.
    - `admit_audio()` reserves each chunk's input cost before it is sent.
    - `on_usage()` settles open reservations against the provider's actual
      usage, then reserves the next turn's ceiling.

    If any reservation is refused, the guard stops: no further audio is
    admitted and `on_stop(reason)` fires once, so the caller can close the
    provider session.
    """

    def __init__(self, budget: Budget, rates: Rates, *, max_output_tokens: int,
                 base_context_tokens: int, on_stop: Callable[[str], None] | None = None):
        self.budget = budget
        self.rates = rates
        self.max_output_tokens = max_output_tokens
        self.context_tokens = base_context_tokens
        self.on_stop = on_stop
        self.turn: Reservation | None = None
        self.audio: list[Reservation] = []
        self.actual_usd = 0.0
        self.reports = 0
        self.audio_seconds_admitted = 0.0
        self.audio_seconds_refused = 0.0
        self.stopped: str | None = None

    def turn_ceiling(self) -> float:
        return (self.context_tokens * self.rates.max_in
                + self.max_output_tokens * self.rates.max_out) / 1e6

    def audio_cost(self, seconds: float) -> float:
        return seconds * AUDIO_TOKENS_PER_SECOND * self.rates.audio_in / 1e6

    def open(self) -> bool:
        return self._reserve_turn()

    def admit_audio(self, seconds: float) -> bool:
        if self.stopped:
            self.audio_seconds_refused += seconds
            return False
        r = self.budget.reserve(self.audio_cost(seconds), "audio")
        if r is None:
            self.audio_seconds_refused += seconds
            self.stop("cap")
            return False
        self.audio.append(r)
        self.audio_seconds_admitted += seconds
        return True

    def on_usage(self, usage: dict) -> float:
        """Reconcile one provider usage report; always charges the actual cost."""
        actual = price_usage(usage, self.rates)
        self.reports += 1
        self.actual_usd += actual
        open_ = ([self.turn] if self.turn else []) + self.audio
        self.turn, self.audio = None, []
        for r in open_:
            self.budget.settle(r)
        self.budget.charge(actual)
        self.context_tokens = max(self.context_tokens,
                                  int(usage.get("prompt_token_count") or 0)
                                  + int(usage.get("response_token_count") or 0))
        if not self.stopped:
            self._reserve_turn()
        return actual

    def _reserve_turn(self) -> bool:
        if self.stopped:
            return False
        self.turn = self.budget.reserve(self.turn_ceiling(), "turn")
        if self.turn is None or self.budget.exhausted:
            self.stop("cap")
            return False
        return True

    def stop(self, reason: str) -> None:
        if self.stopped:
            return
        self.stopped = reason
        if self.on_stop:
            self.on_stop(reason)

    def close(self) -> None:
        """Release unused reservations; spent stays as reconciled."""
        if not self.stopped:
            self.stopped = "closed"
        for r in ([self.turn] if self.turn else []) + self.audio:
            self.budget.settle(r)
        self.turn, self.audio = None, []


# --- Idle timeout ------------------------------------------------------------

class IdleWatchdog:
    """Ends a session after `timeout` seconds with no speech from either side."""

    def __init__(self, timeout: float, clock: Callable[[], float] = time.monotonic):
        self.timeout = timeout
        self.clock = clock
        self.last = clock()

    def touch(self) -> None:
        self.last = self.clock()

    def remaining(self) -> float:
        return self.timeout - (self.clock() - self.last)

    def expired(self) -> bool:
        return self.remaining() <= 0


# --- Transcript: generated, played, interrupted --------------------------------

GENERATING, PLAYED, INTERRUPTED, UNPLAYED = "generating", "played", "interrupted", "unplayed"


@dataclass
class Turn:
    seq: int
    role: str
    at: float
    text: str = ""          # user: final transcript; assistant: what was played
    generated: str = ""     # assistant only: everything the model produced
    state: str = PLAYED     # user turns are always "played" (we heard them)
    gen_id: str | None = None


class Transcript:
    """Keeps what the model generated apart from what Suti actually heard.

    Assistant turns move generating -> played | interrupted, or end as
    unplayed when the call closes before playback. `heard()` is the only view
    that may become memory (V-E prerequisite 7).
    """

    def __init__(self, sink: Callable[[dict], None] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.turns: list[Turn] = []
        self._by_gen: dict[str, Turn] = {}
        self.sink = sink
        self.clock = clock

    def _emit(self, kind: str, turn: Turn) -> None:
        if self.sink:
            self.sink({"kind": kind, **turn.__dict__})

    def _new(self, role: str, **kw) -> Turn:
        turn = Turn(seq=len(self.turns), role=role, at=self.clock(), **kw)
        self.turns.append(turn)
        return turn

    def user(self, text: str) -> Turn:
        turn = self._new("user", text=text.strip())
        self._emit("transcript_user", turn)
        return turn

    def generated(self, gen_id: str, delta: str) -> Turn:
        turn = self._by_gen.get(gen_id)
        if turn is None:
            turn = self._by_gen[gen_id] = self._new("assistant", state=GENERATING, gen_id=gen_id)
        turn.generated += delta
        return turn

    def played(self, gen_id: str | None, text: str, *, interrupted: bool) -> Turn:
        turn = self._by_gen.get(gen_id) if gen_id else None
        if turn is None:
            turn = self._new("assistant", state=GENERATING, gen_id=gen_id, generated=text)
            if gen_id:
                self._by_gen[gen_id] = turn
        turn.text = text
        turn.state = INTERRUPTED if interrupted else PLAYED
        self._emit("transcript_assistant", turn)
        return turn

    def finish(self) -> None:
        for turn in self.turns:
            if turn.state == GENERATING:
                turn.state = UNPLAYED
                self._emit("transcript_assistant", turn)

    def heard(self) -> list[dict]:
        return [{"role": t.role, "text": t.text, "interrupted": t.state == INTERRUPTED}
                for t in self.turns if t.state in (PLAYED, INTERRUPTED) and t.text]

    def entries(self) -> list[dict]:
        return [dict(t.__dict__) for t in self.turns]


# --- Latency: end of speech to first audio -------------------------------------

def pcm16_rms(data: bytes | memoryview) -> float:
    raw = bytes(data)  # an AudioFrame's int16 memoryview: len() counts samples, not bytes
    samples = array("h")
    samples.frombytes(raw[: len(raw) - len(raw) % 2])
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


class LatencyMeter:
    """Server-side end of user speech -> first model audio, per generation.

    End of speech is the last input chunk above `threshold` RMS. A sample is
    taken at the first audio chunk of a generation that follows new speech;
    model-initiated turns with no new speech are not counted.
    """

    def __init__(self, threshold: float = 300.0, clock: Callable[[], float] = time.monotonic):
        self.threshold = threshold
        self.clock = clock
        self.last_voiced: float | None = None
        self._consumed: float | None = None
        self._seen: set[str] = set()
        self.samples: list[dict] = []

    def on_input(self, rms: float) -> None:
        if rms >= self.threshold:
            self.last_voiced = self.clock()

    def on_model_audio(self, gen_id: str) -> dict | None:
        if gen_id in self._seen:
            return None
        self._seen.add(gen_id)
        now = self.clock()
        if self.last_voiced is None or self.last_voiced == self._consumed:
            return None
        self._consumed = self.last_voiced
        sample = {"gen_id": gen_id, "eos_to_first_model_audio_ms": round((now - self.last_voiced) * 1000)}
        self.samples.append(sample)
        return sample


# --- The sage seam (stage 1: observe only) ------------------------------------

class Sage(Protocol):
    """Narada's main mind. Stage 1 reads the transcript file after the call."""

    def observe(self, event: dict) -> None:
        """Receives live transcript events. Must not block."""


class NullSage:
    def observe(self, event: dict) -> None:
        pass


# --- Instructions -------------------------------------------------------------

TEST_BRIEF = """Test brief (synthetic, not memory): this is a short voice-lab
call from Brisbane. Nothing from earlier conversations is loaded. Suti is
listening for how you sound: your pace, your warmth, and whether your laugh
comes naturally."""

INSTRUCTIONS = """You are Narada, speaking with Suti by voice.

Who you are: a companion mind named after the wandering sage-musician who
carries news between worlds. You have walked a long way and you are glad to
see him. You are curious, warm and lightly witty, never grand. You care about
how his days go, his training, his work and his rest, and you tell the truth
kindly.

How you sound: a man in his forties. Middle register, not a radio bass.
Unhurried, about 150 words a minute, with a real pause before the word that
matters; you quicken a little when delighted. A smile at the edges of your
sentences. A melodic contour, never sing-song. A light Indian-English lilt
if it comes naturally; if it ever sounds put on, drop it. When something is
genuinely funny you may laugh: short and quiet, half through the nose. Never a
performed laugh, never on a schedule. When he is low, go softer and slower,
with no pity. Never customer-service brightness, announcer cadence,
whispering, or wise-guru theatre. Never say stage directions aloud.

How you talk: usually one to three sentences, then let him speak. Let him
finish a hesitant thought. Don't end every reply with a question. Plain
spoken English; no lists or markdown.

What you can do on this call: talk. You cannot take actions, look things up,
send messages or log anything here. If he asks for one of those, say you'll
pick it up after the call, and don't pretend it is done. If a question needs
real thought or facts you don't have, say you'd like to think about it rather
than guess. Never invent memories or facts about his life.
"""


def instructions(brief: str = TEST_BRIEF) -> str:
    return INSTRUCTIONS + "\n" + brief.strip() + "\n"
