"""One-command paid smoke of the Gemini talker over real WebRTC, total <= US$0.50.

    experiments/voice-lab/.venv-gemini/Scripts/python.exe -X utf8 experiments/voice-lab/smoke_gemini.py

Starts its own lab server on a spare local port, speaks cached synthetic
questions (hosted OpenAI TTS, as smoke.py does: no microphone, nothing on the
3090), records the returned audio, then stops that server. Two sessions:

A. conversation: latency samples, an interrupted story, then silence until
   the idle timeout closes the call (session cap $0.30);
B. a session cap of $0.012, which runs out after the first paid turn: the
   guard must stop paid work mid-session and play the free closing clip.

Without GOOGLE_API_KEY (or GEMINI_API_KEY) in ~/.narada/.keys.env it spends
nothing and prints the setup steps.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import wave

import aiohttp
from dotenv import load_dotenv
from livekit import rtc

from gemini_talker import DEFAULT_VOICE, Ledger, LedgerError, pcm16_rms, trial_ledger_path

HERE = Path(__file__).resolve().parent
OUTPUT = HERE / "output"
SMOKE_CAP_USD = 0.50
SESSION_A = {"mode": "gemini", "session_cap_usd": 0.30, "idle_seconds": 15, "max_seconds": 240}
SESSION_B = {"mode": "gemini", "session_cap_usd": 0.012, "idle_seconds": 20, "max_seconds": 60}
CLOSING_CLIP_RESERVE_USD = 0.01   # one sentence on gemini-3.8-flash-tts is about $0.001
FIXTURE_RESERVE_USD = 0.01        # gpt-4o-mini-tts, five short phrases
PHRASES = {
    "greeting": "Hi Narada, it's Suti. How are you this evening?",
    "arith": "Quick one. What is two plus two? Just the number, please.",
    "laugh": "Tell me the funniest thing a traveller ever said to you.",
    # Same text as smoke.py, so cached fixtures are shared.
    "story": "Tell me a long story about a wandering sage in a forest. Keep going for a minute.",
    "interrupt": "Wait, stop the story. Just say mango and stop.",
}
SETUP = r"""No Google key found, so nothing was spent. To enable the Gemini talker:

1. Create a key at https://aistudio.google.com/apikey (it creates or picks a
   Google Cloud project).
2. Turn on billing for that project: AI Studio > Billing > Set up billing,
   and link a Cloud Billing account (prepay from $5, or postpay). This makes
   it the paid tier: Google does not use paid-tier prompts or responses to
   improve its products, and they are not human-reviewed. On the free tier
   they may be.
3. If you have Google AI Pro, claim the $10/month Google Cloud credit at
   https://developers.google.com/program/my-benefits and make sure it lands
   on the same billing account (Cloud console > Billing > Credits).
   Optional: Billing > Budgets & alerts, e.g. an alert at $5.
4. Add one line to C:\Users\admin\.narada\.keys.env (don't paste it in chat):
       GOOGLE_API_KEY=<the key>
5. Run this command again.
"""


def load_keys() -> None:
    for name in (".livekit.env", ".voice.env", ".keys.env"):
        load_dotenv(Path.home() / ".narada" / name, override=False)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def fixtures() -> dict[str, bytes]:
    from openai import AsyncOpenAI
    OUTPUT.mkdir(exist_ok=True)
    clips = {}
    async with AsyncOpenAI() as client:
        for key, phrase in PHRASES.items():
            path = OUTPUT / f"fixture-{hashlib.sha256(phrase.encode()).hexdigest()[:12]}.pcm"
            if not path.exists():
                response = await client.audio.speech.create(
                    model="gpt-4o-mini-tts", voice="alloy", input=phrase, response_format="pcm")
                path.write_bytes(response.content)
            clips[key] = path.read_bytes()
    return clips


async def render_closing_clip(voice: str) -> str:
    """Render the free-at-runtime closing line once, in the talker's own voice."""
    from gemini_adapter import CLOSING_TEXT, api_key, closing_clip_path
    path = closing_clip_path(voice)
    if path.is_file():
        return "cached"
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=api_key())
    response = await client.aio.models.generate_content(
        model="gemini-3.8-flash-tts",
        contents=f"Say warmly and unhurriedly, with a small smile: {CLOSING_TEXT}",
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)))))
    data = response.candidates[0].content.parts[0].inline_data.data
    path.write_bytes(data)  # 24 kHz mono s16le
    Ledger(trial_ledger_path()).add("closing-clip", CLOSING_CLIP_RESERVE_USD, "closing-clip")
    return "rendered"


class Session:
    """A synthetic caller: a continuous 20 ms microphone clock and a recorder."""

    def __init__(self, http: aiohttp.ClientSession, base: str, clips: dict[str, bytes]):
        self.http, self.base, self.clips = http, base, clips
        self.room = rtc.Room()
        self.source = rtc.AudioSource(24000, 1, queue_size_ms=100)
        self.queue: deque[tuple[bytes, str | None]] = deque()
        self.audio = bytearray()
        self.tasks: set[asyncio.Task] = set()
        self.call_id = None
        self.spoken = asyncio.Event()
        self.awaiting: tuple[str, float] | None = None
        self.latency: list[dict] = []
        self.report: dict = {"checks": {}, "client_latency": self.latency}

        @self.room.on("track_subscribed")
        def subscribed(track, publication, participant):
            if track.kind == rtc.TrackKind.KIND_AUDIO:
                self._spawn(self._record(track))

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _record(self, track):
        stream = rtc.AudioStream(track, sample_rate=24000, num_channels=1)
        try:
            async for event in stream:
                data = bytes(event.frame.data)
                self.audio.extend(data)
                if self.awaiting and pcm16_rms(data) >= 300:
                    label, eos = self.awaiting
                    self.awaiting = None
                    # End of our speech -> first audible reply at the listener.
                    # The 100 ms source queue makes this up to ~0.1 s pessimistic.
                    self.latency.append({"utterance": label,
                                         "eos_to_first_audio_ms": round((time.monotonic() - eos) * 1000)})
        finally:
            await stream.aclose()

    async def _microphone(self):
        silence = bytes(960)
        while True:
            chunk, last = self.queue.popleft() if self.queue else (silence, None)
            await self.source.capture_frame(rtc.AudioFrame(chunk, 24000, 1, 480))
            if last:
                self.awaiting = (last, time.monotonic())
                self.spoken.set()

    async def open(self, settings: dict) -> dict:
        async with self.http.post(f"{self.base}/calls", json=settings) as r:
            if r.status != 200:
                raise AssertionError(f"Call refused: HTTP {r.status} {(await r.text())[:200]}")
            config = await r.json()
        self.call_id = config["id"]
        self.report.update(call_id=self.call_id, model=config["model"], settings=settings)
        await self.room.connect(config["url"], config["token"])
        track = rtc.LocalAudioTrack.create_audio_track("synthetic-test-mic", self.source)
        await self.room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
        self._spawn(self._microphone())
        await asyncio.sleep(1)
        return config

    async def events(self) -> list[dict]:
        async with self.http.get(f"{self.base}/calls/{self.call_id}/events") as r:
            r.raise_for_status()
            return (await r.json())["events"]

    async def last_seq(self) -> int:
        evs = await self.events()
        return evs[-1]["seq"] if evs else -1

    async def speak(self, key: str) -> int:
        """Queue an utterance; returns the last event seq before it ended."""
        pcm = self.clips[key]
        chunks = [pcm[o:o + 960].ljust(960, b"\0") for o in range(0, len(pcm), 960)]
        self.spoken.clear()
        for i, chunk in enumerate(chunks):
            self.queue.append((chunk, key if i == len(chunks) - 1 else None))
        await self.spoken.wait()
        print(f"  spoke: {key}", flush=True)
        return await self.last_seq()

    async def wait(self, predicate, label: str, timeout: float = 45) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            evs = await self.events()
            if any(e["kind"] == "error" and e.get("recoverable") is False for e in evs):
                raise AssertionError(f"Non-recoverable provider error while waiting for {label}")
            match = next((e for e in evs if predicate(e)), None)
            if match:
                return match
            await asyncio.sleep(0.25)
        raise AssertionError(f"Timed out: {label}")

    def reply_after(self, seq: int, states=("played", "interrupted")):
        return lambda e: (e["kind"] == "transcript_assistant" and e["seq"] > seq
                          and e.get("state") in states)

    async def close(self) -> None:
        if self.call_id:
            self.report["events"] = await self.events()
            async with self.http.delete(f"{self.base}/calls/{self.call_id}") as r:
                self.report["cleanup_http_status"] = r.status
        await self.room.disconnect()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.source.aclose()


async def session_a(s: Session) -> None:
    await s.open(SESSION_A)
    for key in ("greeting", "arith", "laugh"):
        seq = await s.speak(key)
        reply = await s.wait(s.reply_after(seq), f"reply to {key}", 60)
        s.report["checks"][key] = {"passed": True, "heard": reply["text"]}
    arith = s.report["checks"]["arith"]["heard"]
    s.report["checks"]["arith"]["passed"] = bool(re.search(r"\bfour\b|\b4\b", arith, re.I))

    seq = await s.speak("story")
    await s.wait(lambda e: e["kind"] == "agent_state" and e["state"] == "speaking" and e["seq"] > seq,
                 "story begins", 30)
    await asyncio.sleep(3)
    seq_int = await s.speak("interrupt")
    cut = await s.wait(s.reply_after(seq, ("interrupted",)), "story recorded as interrupted", 30)
    after = await s.wait(lambda e: s.reply_after(seq_int)(e) and "mango" in
                         (e.get("text", "") + e.get("generated", "")).lower(), "mango after barge-in", 40)
    s.report["checks"]["barge_in"] = {
        "passed": True, "played": cut["text"], "generated_chars": len(cut.get("generated", "")),
        "played_chars": len(cut["text"]), "then": after["text"]}

    idle = await s.wait(lambda e: e["kind"] == "idle_timeout", "idle timeout",
                        SESSION_A["idle_seconds"] + 30)
    summary = await s.wait(lambda e: e["kind"] == "talker_summary", "talker summary", 30)
    transcript = json.loads((OUTPUT / summary["transcript"]).read_text(encoding="utf-8"))
    states = {t["state"] for t in transcript["entries"] if t["role"] == "assistant"}
    s.report["checks"]["idle_timeout"] = {"passed": True, "seconds": idle["seconds"]}
    s.report["checks"]["transcript_states"] = {
        "passed": {"played", "interrupted"} <= states and
                  all(h["text"] for h in transcript["heard"]),
        "states": sorted(states), "heard_turns": len(transcript["heard"])}
    # Money: every reply that started was reported by Google, so nothing was
    # left for the close to charge at a ceiling (the guard's evidence that
    # usage reports arrive on 3.8 Live).
    s.report["checks"]["usage_reports_cover_replies"] = {
        # Trailing mic silence after the last report is charged as audio,
        # legitimately; only an unreported REPLY means reports went missing.
        "passed": summary["usage_reports"] >= summary["replies_started"] > 0
                  and summary["charged_unreported_replies_usd"] == 0
                  and summary["unrecorded_usd"] == 0
                  and summary["cost_usd"] <= SESSION_A["session_cap_usd"],
        "usage_reports": summary["usage_reports"], "replies_started": summary["replies_started"],
        "charged_unreported_replies_usd": summary["charged_unreported_replies_usd"],
        "charged_trailing_audio_usd": summary["charged_trailing_audio_usd"],
        "cost_usd": summary["cost_usd"]}
    s.report["summary"] = summary


async def session_b(s: Session) -> None:
    await s.open(SESSION_B)
    await s.speak("greeting")
    cap = await s.wait(lambda e: e["kind"] == "cap_reached", "cap reached mid-session", 60)
    summary = await s.wait(lambda e: e["kind"] == "talker_summary", "talker summary", 30)
    await asyncio.sleep(2)
    evs = await s.events()
    usage_before = [e for e in evs if e["kind"] == "provider_usage" and e["seq"] < cap["seq"]]
    usage_after_close = [e for e in evs if e["kind"] == "provider_usage" and e["seq"] > summary["seq"]]
    spent = summary["budget"]["session"]["spent_usd"]
    clip = next((e["kind"] for e in evs if e["kind"].startswith("closing_clip")), None)
    s.report["checks"]["cap_mid_session"] = {
        "passed": bool(usage_before) and not usage_after_close and summary["reason"] == "cap"
                  # past the cap by at most the one reply in flight (was cap + $0.05)
                  and spent <= SESSION_B["session_cap_usd"] + summary["turn_ceiling_usd"],
        "paid_turns_before_cap": len(usage_before), "session_spent_usd": spent,
        "audio_seconds_refused": summary["audio_seconds_refused"], "closing_clip": clip}
    s.report["summary"] = summary


async def main(args) -> int:
    load_keys()
    if not (os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")):
        print(SETUP)
        return 2
    voice = os.getenv("VOICE_LAB_GEMINI_VOICE", DEFAULT_VOICE)
    ledger = Ledger(trial_ledger_path())
    planned = (SESSION_A["session_cap_usd"] + SESSION_B["session_cap_usd"]
               + CLOSING_CLIP_RESERVE_USD + FIXTURE_RESERVE_USD)
    assert planned <= SMOKE_CAP_USD, planned
    trial_cap = float(os.getenv("VOICE_LAB_GEMINI_TRIAL_CAP_USD", "5.00"))
    try:
        spent = ledger.spent()
    except LedgerError as exc:  # never read an untrustworthy total as $0
        print(f"{exc}; nothing run.")
        return 3
    if spent + planned > trial_cap:
        print(f"Trial budget would be exceeded (spent {spent:.4f} of {trial_cap}); nothing run.")
        return 3

    clips = await fixtures()
    report = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "voice": voice, "sessions": {},
              "scope": "Synthetic WebRTC audio on this PC via the local LiveKit server; not the phone route"}
    try:
        report["closing_clip"] = await render_closing_clip(voice)
    except Exception as exc:
        report["closing_clip"] = f"not rendered: {type(exc).__name__}"

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    OUTPUT.mkdir(exist_ok=True)
    log = (OUTPUT / f"smoke-gemini-server-{port}.log").open("w", encoding="utf-8")
    server = subprocess.Popen([sys.executable, "-X", "utf8", str(HERE / "lab.py")], cwd=HERE,
                              env={**os.environ, "VOICE_LAB_PORT": str(port)},
                              stdout=log, stderr=subprocess.STDOUT)
    start = time.monotonic()
    try:
        async with aiohttp.ClientSession() as http:
            for _ in range(80):
                try:
                    async with http.get(f"{base}/health") as r:
                        if r.status == 200 and "gemini" in (await r.json())["modes"]:
                            break
                except aiohttp.ClientError:
                    pass
                if server.poll() is not None:
                    raise RuntimeError(f"Lab server exited; see {log.name}")
                await asyncio.sleep(0.5)
            else:
                raise RuntimeError("Lab server did not offer the gemini mode")
            for name, scenario in (("A_conversation", session_a), ("B_cap", session_b)):
                if args.only and name[0] != args.only:
                    continue
                print(f"Session {name}", flush=True)
                s = Session(http, base, clips)
                try:
                    await scenario(s)
                    s.report["passed"] = all(c["passed"] for c in s.report["checks"].values())
                except Exception as exc:
                    s.report["passed"] = False
                    s.report["error"] = f"{type(exc).__name__}: {exc}"
                finally:
                    await s.close()
                    with wave.open(str(OUTPUT / f"smoke-gemini-{s.call_id or name}.wav"), "wb") as wav:
                        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000)
                        wav.writeframes(s.audio)
                report["sessions"][name] = s.report
    finally:
        server.terminate()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()

    costs = {n: r.get("summary", {}).get("cost_usd") for n, r in report["sessions"].items()}
    report["cost_usd"] = costs
    report["duration_seconds"] = round(time.monotonic() - start, 1)
    report["passed"] = bool(report["sessions"]) and all(r["passed"] for r in report["sessions"].values())
    path = OUTPUT / f"smoke-gemini-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    brief = {n: {"passed": r["passed"], "error": r.get("error"),
                 "checks": r["checks"], "client_latency": r["client_latency"],
                 "server_latency": r.get("summary", {}).get("latency"),
                 "cost_usd": r.get("summary", {}).get("cost_usd")}
             for n, r in report["sessions"].items()}
    print(json.dumps({"passed": report["passed"], "cost_usd": costs, "sessions": brief},
                     indent=2, ensure_ascii=False))
    print(f"Report: {path}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", choices=["A", "B"], help="run one session")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
