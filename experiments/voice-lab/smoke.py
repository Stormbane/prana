"""Paid, bounded synthetic-audio check of the isolated lab over real WebRTC.

Run with lab.py already listening. Does not use the machine's microphone.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import re
import time
import wave

import aiohttp
from dotenv import load_dotenv
from livekit import rtc
from openai import AsyncOpenAI

HERE = Path(__file__).resolve().parent
OUTPUT = HERE / "output"
BASE = "http://127.0.0.1:8898"
PHRASES = {
    "slow": "Start a slow lookup, please.",
    "question": "While that runs, what is two plus two? Answer with just the number.",
    "failure": "Please start a failing lookup so we can test failure.",
    "cancel": "Cancel that lookup, please. Stop the background lookup now.",
    "story": "Tell me a long story about a wandering sage in a forest. Keep going for a minute.",
    "interrupt": "Wait, stop the story. Just say mango and stop.",
}


async def fixtures():
    load_dotenv(Path.home() / ".narada/.voice.env", override=False)
    OUTPUT.mkdir(exist_ok=True)
    result = {}
    async with AsyncOpenAI() as client:
        for key, phrase in PHRASES.items():
            digest = hashlib.sha256(phrase.encode()).hexdigest()[:12]
            path = OUTPUT / f"fixture-{digest}.pcm"
            if not path.exists():
                response = await client.audio.speech.create(
                    model="gpt-4o-mini-tts", voice="alloy", input=phrase, response_format="pcm")
                path.write_bytes(response.content)
            result[key] = path.read_bytes()
    return result


async def main(mode):
    clips = await fixtures()
    room = rtc.Room()
    tasks = set()
    audio = bytearray()
    audio_ready = asyncio.Event()
    report = {"mode": mode, "checks": {}, "utterances": [], "scope": "Synthetic WebRTC audio; no human listening score"}
    call_id = None
    start = time.monotonic()
    source = rtc.AudioSource(24000, 1, queue_size_ms=100)

    async def record(track):
        stream = rtc.AudioStream(track, sample_rate=24000, num_channels=1)
        try:
            async for event in stream:
                audio.extend(bytes(event.frame.data))
                audio_ready.set()
        finally:
            await stream.aclose()

    @room.on("track_subscribed")
    def subscribed(track, publication, participant):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            task = asyncio.create_task(record(track))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

    async with aiohttp.ClientSession() as http:
        async def events():
            async with http.get(f"{BASE}/calls/{call_id}/events") as response:
                response.raise_for_status()
                return (await response.json())["events"]

        async def wait_for(predicate, label, timeout=45):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                evs = await events()
                if any(e["kind"] == "error" and not e.get("recoverable") for e in evs):
                    raise AssertionError("Non-recoverable speech error; inspect call events")
                match = next((e for e in evs if predicate(e)), None)
                if match:
                    return match
                await asyncio.sleep(0.25)
            raise AssertionError(f"Timed out: {label}")

        async def speak(key):
            pcm = clips[key]
            t0 = time.monotonic() - start
            # 20 ms frames, paced by AudioSource's bounded queue.
            for offset in range(0, len(pcm), 960):
                chunk = pcm[offset:offset+960].ljust(960, b"\0")
                await source.capture_frame(rtc.AudioFrame(chunk, 24000, 1, 480))
            await source.wait_for_playout()
            t1 = time.monotonic() - start
            report["utterances"].append({"phrase": PHRASES[key], "start": round(t0,3), "end": round(t1,3)})
            print(f"Spoke: {key}", flush=True)

        try:
            async with http.post(f"{BASE}/calls", json={"mode": mode}) as response:
                response.raise_for_status()
                config = await response.json()
            call_id = config["id"]
            report["call_id"] = call_id
            report["model"] = config["model"]
            await room.connect(config["url"], config["token"])
            track = rtc.LocalAudioTrack.create_audio_track("synthetic-test-mic", source)
            await room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
            await asyncio.sleep(1)
            await speak("slow")
            began = await wait_for(lambda e:e["kind"]=="lookup_started", "slow lookup start")
            await wait_for(lambda e:e["kind"]=="conversation" and e["role"]=="assistant" and e["seconds"]>began["seconds"], "lookup acknowledgment")
            await speak("question")
            answer = await wait_for(lambda e:e["kind"]=="conversation" and e["role"]=="assistant" and e["seconds"]>began["seconds"] and re.search(r"\bfour\b|\b4\b", e.get("text", ""), re.I), "answer during lookup", 25)
            completed = await wait_for(lambda e:e["kind"]=="lookup_completed", "lookup completion")
            assert answer["seconds"] < completed["seconds"], "Answer arrived only after lookup completed"
            report["checks"]["answered_while_tool_running"] = {"passed": True, "answer_after_start_seconds": round(answer["seconds"]-began["seconds"],3)}
            result = await wait_for(lambda e:e["kind"]=="conversation" and e["role"]=="assistant" and e["seconds"]>completed["seconds"] and "mango" in e.get("text", "").lower(), "spoken final result")
            report["checks"]["delivered_tool_result"] = {"passed": True, "seconds_after_completion": round(result["seconds"]-completed["seconds"],3)}
            await asyncio.wait_for(audio_ready.wait(), 10)

            await speak("failure")
            failed = await wait_for(lambda e:e["kind"]=="lookup_failed", "simulated failure")
            # A pending acknowledgment can finish AFTER the error arrives, and
            # can quote the future failure wording. Wait for the framework's
            # final-result speech completion before judging the transcript.
            failure_played = await wait_for(lambda e:e["kind"]=="tool" and e.get("update",{}).get("type")=="tool_reply_updated" and e["update"].get("status")=="completed" and failed["call_id"]+"_final" in e["update"].get("update_ids",[]), "failure result finished playing")
            failure_replies = [e for e in await events() if e["kind"]=="conversation" and e["role"]=="assistant" and failed["seq"] < e["seq"] < failure_played["seq"]]
            assert failure_replies, "Final failure playback had no assistant transcript"
            failure_reply = failure_replies[-1]
            assert re.search(r"\b(?:it|lookup|simulation|test) (?:has |had )?failed\b|\breturned an error\b|\bno result is available\b",failure_reply["text"],re.I), "Final reply did not affirm failure"
            assert not re.search(r"still running|if (?:it|the lookup) fails|can.t confirm",failure_reply["text"],re.I), "Final reply described a pending lookup"
            report["checks"]["reported_failure"] = {"passed": True, "text": failure_reply["text"]}

            await speak("slow")
            began2 = await wait_for(lambda e:e["kind"]=="lookup_started" and e["seconds"]>failed["seconds"], "second lookup")
            await wait_for(lambda e:e["kind"]=="conversation" and e["role"]=="assistant" and e["seconds"]>began2["seconds"], "second acknowledgment")
            await speak("cancel")
            cancelled = await wait_for(lambda e:e["kind"]=="lookup_cancelled" and e["seconds"]>began2["seconds"], "explicit cancellation", 20)
            report["checks"]["explicit_cancellation"] = {"passed": True}
            await wait_for(lambda e:e["kind"]=="conversation" and e["role"]=="assistant" and e["seconds"]>cancelled["seconds"], "cancellation acknowledgment")

            marker = max(e["seconds"] for e in await events())
            await speak("story")
            await wait_for(lambda e:e["kind"]=="agent_state" and e["state"]=="speaking" and e["seconds"]>marker, "story begins")
            await asyncio.sleep(2)
            await speak("interrupt")
            interrupted = await wait_for(lambda e:e["kind"]=="conversation" and e["role"]=="assistant" and e["seconds"]>marker and e.get("interrupted"), "story interrupted", 20)
            await wait_for(lambda e:e["kind"]=="conversation" and e["role"]=="assistant" and e["seconds"]>interrupted["seconds"] and "mango" in e.get("text", "").lower(), "new response after interruption")
            report["checks"]["barge_in"] = {"passed": True}
            all_events = await events()
            assert not any(e["kind"]=="lookup_completed" and e.get("call_id")==began2["call_id"] for e in all_events)
            assert any(e["kind"]=="conversation" and e["role"]=="user" for e in all_events)
            assert audio and any(audio), "No nonzero received audio"
            report["checks"]["audio_round_trip"] = {"passed": True, "received_seconds": round(len(audio)/48000,2)}
            report["passed"] = True
        except Exception as exc:
            report["passed"] = False
            report["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            if call_id:
                report["events"] = await events()
                async with http.delete(f"{BASE}/calls/{call_id}") as response:
                    report["cleanup_http_status"] = response.status
            await room.disconnect()
            for task in list(tasks):
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await source.aclose()
            report["duration_seconds"] = round(time.monotonic()-start,2)
            path = OUTPUT / f"smoke-{mode}-{call_id or 'failed-start'}.json"
            path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            with wave.open(str(path.with_suffix(".wav")), "wb") as wav:
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000); wav.writeframes(audio)
            print(json.dumps({k:v for k,v in report.items() if k not in ("events","utterances")}, indent=2))
            print(f"Report: {path}")
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["semantic", "server"], default="semantic")
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(main(args.mode)) else 1)
