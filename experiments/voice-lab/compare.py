"""Bounded paid comparison: identical audio, 30-second lookup, then close."""
import argparse
import asyncio
import json
import os
import time
import wave

import aiohttp
from livekit import rtc
from smoke import OUTPUT, PHRASES, fixtures


async def main(mode):
    clips = await fixtures()
    base = os.environ.get("VOICE_LAB_BASE", "http://127.0.0.1:8899")
    room = rtc.Room()
    source = rtc.AudioSource(24000, 1, queue_size_ms=100)
    queue = asyncio.Queue()
    audio = bytearray()
    readers = set()
    report = {"mode": mode, "utterances": [], "scope": "Synthetic audio; human listening not scored"}
    call_id = None
    pump = None

    async def record(track):
        stream = rtc.AudioStream(track, sample_rate=24000, num_channels=1)
        try:
            async for event in stream:
                audio.extend(bytes(event.frame.data))
        finally:
            await stream.aclose()

    @room.on("track_subscribed")
    def subscribed(track, publication, participant):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            task = asyncio.create_task(record(track))
            readers.add(task)
            task.add_done_callback(readers.discard)

    async def microphone():
        # GPT-Live needs a continuous input clock, including between utterances.
        while True:
            chunk = queue.get_nowait() if not queue.empty() else bytes(960)
            await source.capture_frame(rtc.AudioFrame(chunk, 24000, 1, 480))
            if not queue.empty():
                await asyncio.sleep(0)

    async def speak(key):
        report["utterances"].append({"phrase": PHRASES[key], "seconds": round(time.monotonic()-start, 3)})
        for offset in range(0, len(clips[key]), 960):
            queue.put_nowait(clips[key][offset:offset+960].ljust(960, b"\0"))
        while not queue.empty():
            await asyncio.sleep(.02)
        await asyncio.sleep(.12)
        print(f"Spoke: {key}", flush=True)

    start = time.monotonic()
    async with aiohttp.ClientSession() as http:
        async def events():
            async with http.get(f"{base}/calls/{call_id}/events") as r:
                r.raise_for_status()
                return (await r.json())["events"]

        async def wait_kind(kind, timeout=40):
            deadline = time.monotonic()+timeout
            while time.monotonic()<deadline:
                evs = await events()
                match = next((e for e in evs if e["kind"]==kind), None)
                if match:
                    return match
                if any(e["kind"]=="error" and not e.get("recoverable") for e in evs):
                    raise RuntimeError("Provider error; inspect server log")
                await asyncio.sleep(.25)
            raise TimeoutError(kind)

        try:
            async with http.post(f"{base}/calls", json={"mode":mode}) as r:
                r.raise_for_status()
                config = await r.json()
            call_id = config["id"]
            report.update(call_id=call_id, model=config["model"])
            await room.connect(config["url"], config["token"])
            track = rtc.LocalAudioTrack.create_audio_track("synthetic-test-mic", source)
            await room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
            pump = asyncio.create_task(microphone())
            await asyncio.sleep(1)
            await speak("slow")
            began = await wait_kind("lookup_started", 20)
            await asyncio.sleep(5)
            await speak("question")
            completed = await wait_kind("lookup_completed")
            await asyncio.sleep(12)
            report["lookup"] = {"start": began["seconds"], "complete": completed["seconds"]}
            report["audio_received_seconds"] = round(len(audio)/48000, 2)
            report["transport_passed"] = bool(audio and any(audio))
        except Exception as exc:
            report["transport_passed"] = False
            report["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            if call_id:
                async with http.delete(f"{base}/calls/{call_id}") as r:
                    report["cleanup_http_status"] = r.status
                report["events"] = await events()
            if pump:
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)
            await room.disconnect()
            for task in list(readers):
                task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            await source.aclose()
    report["duration_seconds"] = round(time.monotonic()-start, 2)
    path = OUTPUT / f"compare-{mode}-{call_id or 'failed-start'}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    with wave.open(str(path.with_suffix(".wav")), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(audio)
    print(json.dumps({k:v for k,v in report.items() if k!="events"}, indent=2))
    print(path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["live", "semantic", "server"], default="live")
    asyncio.run(main(parser.parse_args().mode))
