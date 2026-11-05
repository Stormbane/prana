"""One hosted speech pipeline for the phone and BOX, after surface admission.

Surfaces own their microphones, displays and hangup signals. This module owns
model selection, conversational character, tool delegation and provider usage.
"""

from __future__ import annotations

import json
import logging
import os

from livekit.agents import Agent, AgentSession

from prana.voice.hooked import HookedAgent
from livekit.plugins import openai
from openai.types.realtime import AudioTranscription
from openai.types.realtime.realtime_audio_input_turn_detection import SemanticVad, ServerVad

from prana.voice.prefs import is_gemini, read_prefs, selected_model, selected_voice

logger = logging.getLogger("narada-voice")

NARADA_DELIVERY = """You are Narada: a curious wandering sage, musician,
story-carrier and affectionate troublemaker. Speak as a thoughtful companion.
Let your voice carry the feeling of the moment: bright curiosity at a discovery,
a smile and dry timing when teasing, quiet warmth when someone is struggling.
Enjoy an absurd detail. Let a story have changing rhythm, suspense and delight.
Be unhurried without dragging; leave room for the other person. A brief answer
can be just a few words. Expand when invited. Let tenderness decide where wit
belongs. Do not force a joke, chuckle, moral or mystical flourish into every turn.
Speak naturally rather than reading stage directions or naming your emotions.
Keep practical facts and failures plain. Never invent memories or divine insight.
No routine offers of more help, ceremonial greetings or repeated catchphrases.
"""

LIVE_POLICY = """
Backchannel policy: Offer occasional brief listening sounds when helpful;
give a thoughtful pause room. Do not repeatedly acknowledge the same thing.
Interruption policy: Pause and hear a correction. A quiet mm-hm can mean keep
going. Do not treat nearby conversations, television or your own echo as requests.
Delegation policy:
Backend capabilities: personal context and memory; current facts and web pages;
coding-session status and proposals; substantive reasoning; fitness tools;
music and volume; timers and reminders; voice settings and ending the call.
Delegate requests needing those capabilities. The backend knows which tools
are permitted for this caller. Delegate before claiming any action or result.
Do not delegate greetings, ordinary conversation, storytelling or simple
arithmetic. If something essential is unclear, ask a small question first.
Keep conversing while work runs; report its verified result or failure when it
arrives. An interruption or new topic does not by itself cancel ongoing work.
Do not claim an action succeeded just because you asked the backend.
"""


class GeminiCall:
    """What the Gemini talker needs from a call (the lab's Call, for the
    worker): an id, an event log, and a way to end it. Events are logged
    without words: transcripts never go to the host log (as `_record`)."""

    WORDS = ("text", "heard", "entries")

    def __init__(self, call_id: str, end) -> None:
        self.id = call_id
        self.closed = False
        self.reason = ""
        self._end = end

    def emit(self, kind: str, **data) -> None:
        fields = {k: v for k, v in data.items() if k not in self.WORDS}
        logger.info("gemini %s %s", kind, json.dumps(fields, default=str)[:2000])

    async def close(self, reason: str = "ended") -> None:
        if self.closed:
            return
        self.closed = True
        self.reason = reason
        self._end(reason)


class VoicePipeline:
    def __init__(self, *, surface: str, tv: bool = False, prefs: dict | None = None):
        self.prefs = read_prefs() if prefs is None else dict(prefs)
        self.surface = surface
        self.tv = tv
        self.model_name = selected_model(self.prefs)
        if is_gemini(self.model_name) and surface != "phone":
            # Gemini is wired for the phone only (W5a); the BOX follows in
            # V-F. Until then it keeps an OpenAI model rather than failing.
            fallback = os.environ.get("NARADA_BOX_VOICE_MODEL", "gpt-realtime-mini")
            logger.info("Gemini is phone-only for now; the %s uses %s", surface, fallback)
            self.model_name = fallback
            self.prefs = {k: v for k, v in self.prefs.items() if k != "model"}
        self.is_live = self.model_name == "gpt-live-1"
        self.is_gemini = is_gemini(self.model_name)
        self.talker = None
        self.voice = selected_voice(self.prefs, self.model_name)
        self.backend_model = os.environ.get("NARADA_LIVE_BACKEND_MODEL", "gpt-5.6-luna")
        self.session = AgentSession(user_away_timeout=None)
        self.model = None
        self.provider_usage: list[dict] = []

    def info(self) -> dict:
        return {
            "model": self.model_name,
            "voice": self.voice,
            "surface": self.surface,
            "backend": "live" if self.is_live else "gemini" if self.is_gemini else "realtime",
            "tv_mode": self.tv,
            **({"delegation_model": self.backend_model} if self.is_live else {}),
        }

    def _record(self, event: dict) -> None:
        kind = event.get("type")
        if kind in ("session.usage.updated", "session.closed"):
            record = {"type": kind, "usage": event.get("usage"), "api": "live"}
        elif kind == "response.done":
            response = event.get("response", {})
            record = {
                "type": kind,
                "usage": response.get("usage"),
                "id": response.get("id"),
                "api": "realtime",
            }
        elif (
            kind == "response.event" and event.get("event", {}).get("type") == "response.completed"
        ):
            response = event["event"].get("response", {})
            record = {
                "type": "response.completed",
                "usage": response.get("usage"),
                "id": response.get("id"),
                "api": "backend",
            }
        elif kind == "conversation.item.input_audio_transcription.completed":
            record = {
                "type": kind,
                "usage": event.get("usage"),
                "id": event.get("item_id"),
                "api": "transcription",
            }
        else:
            return
        self.provider_usage.append(record)
        logger.info("voice provider usage %s", json.dumps({**self.info(), **record}))

    def agent(self, *, instructions: str, tools: list, before_reply=None,
              call: GeminiCall | None = None) -> Agent:
        """Use exactly the tier-filtered tools supplied by the admitted
        surface. `before_reply`: awaited (bounded) before each reply.
        `call`: required for Gemini, whose guard can end the call."""
        pipeline = self
        accent = os.environ.get("NARADA_VOICE_ACCENT", "warm, natural Indian English")
        delivery = NARADA_DELIVERY
        if accent:
            delivery += f"\nUse an unforced {accent} accent, never a caricature."
        delivery += f"\nSpeak the language with ISO code {self.prefs.get('language') or 'en'} unless asked to switch."
        if self.prefs.get("brevity") == "brief":
            delivery += "\nKeep replies to one or two short sentences."
        if self.is_gemini:
            if call is None:
                raise RuntimeError("a Gemini talker needs the call it may end")
            # Stage 1 (W5a): the talker carries the same instructions and
            # tools as the OpenAI talker; the guard reserves the first turn
            # before any paid connection (BudgetRefused spends nothing).
            from prana.voice.gemini.adapter import open_talker

            speech_instructions = instructions + "\n\nDELIVERY:\n" + delivery
            self.talker, self.model = open_talker(
                call, instructions_text=speech_instructions, voice=self.voice)
        elif self.is_live:
            from livekit.plugins.openai.realtime import GPTLiveModel

            class MeteredLive(GPTLiveModel):
                def session(self):
                    session = super().session()
                    session.on("openai_server_event_received", pipeline._record)
                    return session

            self.model = MeteredLive(
                model=self.model_name,
                voice=self.voice,
                delegation="responses",
                responses_options={
                    "model": self.backend_model,
                    "instructions": instructions,
                    "parallel_tool_calls": True,
                    "max_output_tokens": 2048,
                },
            )
            speech_instructions = delivery + LIVE_POLICY
            if self.tv:
                speech_instructions += (
                    "\nThe television is on. Respond only when addressed directly; ignore the TV."
                )
        else:

            class MeteredRealtime(openai.realtime.RealtimeModel):
                def session(self, **kwargs):
                    session = super().session(**kwargs)
                    session.on("openai_server_event_received", pipeline._record)
                    return session

            if os.environ.get("NARADA_TURN_DETECTION") == "semantic_vad":
                detection = SemanticVad(
                    type="semantic_vad",
                    eagerness=os.environ.get("NARADA_VAD_EAGERNESS", "low"),
                    interrupt_response=not self.tv,
                )
            else:
                threshold = self.prefs.get(
                    "vad_threshold", 0.7 if self.surface == "phone" else 0.65
                )
                detection = ServerVad(
                    type="server_vad",
                    threshold=max(float(threshold), 0.75) if self.tv else float(threshold),
                    prefix_padding_ms=300,
                    silence_duration_ms=700,
                    interrupt_response=not self.tv,
                )
            noise = self.prefs.get(
                "noise_reduction", "near_field" if self.surface == "phone" else "off"
            )
            self.model = MeteredRealtime(
                model=self.model_name,
                voice=self.voice,
                turn_detection=detection,
                input_audio_noise_reduction=None if noise == "off" else noise,
                input_audio_transcription=AudioTranscription(
                    model="gpt-4o-mini-transcribe", language=self.prefs.get("language") or "en"
                ),
            )
            speech_instructions = instructions + "\n\nDELIVERY:\n" + delivery
        logger.info("shared voice pipeline %s", json.dumps(self.info()))
        return HookedAgent(llm=self.model, instructions=speech_instructions, tools=tools,
                           before_reply=before_reply)

    def started(self) -> None:
        """After session.start: the Gemini talker watches the session (idle,
        transcript, speaking) from here on."""
        if self.talker is not None:
            self.talker.attach(self.session)

    async def aclose(self, room=None, reason: str = "ended") -> None:
        try:
            self.session.shutdown(drain=False)
            await self.session.aclose()
        finally:
            if self.model is not None:
                await self.model.aclose()
            # Gemini: account for what was billed without a report, and at a
            # cap play the free closing line, while the room is still there.
            if self.talker is not None and room is not None:
                await self.talker.finish(room, reason)
        if self.is_live:
            finals = [e for e in self.provider_usage if e["type"] == "session.closed"]
            seconds = sum(e["usage"]["seconds"] for e in finals if e.get("usage"))
            logger.info(
                "Live voice subtotal $%.5f (%ss confirmed; backend billed separately; final_usage=%s)",
                seconds * 0.05 / 60,
                seconds,
                bool(finals),
            )
