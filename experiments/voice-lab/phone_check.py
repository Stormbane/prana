"""Paid, bounded test of the deployed phone dispatch and its read-only self tool."""
import asyncio
import hashlib
import json
import time
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from livekit import api, rtc
from openai import AsyncOpenAI

HERE = Path(__file__).resolve().parent


async def main():
    for name in (".livekit.env", ".voice.env"):
        load_dotenv(Path.home()/".narada"/name)
    phrase = "Which voice model are you running? Check your about myself tool."
    path = HERE/"output"/("fixture-"+hashlib.sha256(phrase.encode()).hexdigest()[:12]+".pcm")
    if not path.exists():
        async with AsyncOpenAI() as client:
            response = await client.audio.speech.create(model="gpt-4o-mini-tts", voice="alloy", input=phrase, response_format="pcm")
            path.write_bytes(response.content)
    pcm = path.read_bytes()
    token = json.loads((Path.home()/".narada/.brain-tokens.json").read_text())["app"]
    room = rtc.Room()
    source = rtc.AudioSource(24000, 1, queue_size_ms=100)
    received = bytearray()
    tasks = set()
    transcripts = []
    config = None
    pump = None

    async def record(track):
        stream = rtc.AudioStream(track, sample_rate=24000, num_channels=1)
        try:
            async for event in stream:
                received.extend(bytes(event.frame.data))
        finally:
            await stream.aclose()

    @room.on("track_subscribed")
    def track(track, *_):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            task = asyncio.create_task(record(track))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

    @room.on("transcription_received")
    def transcript(segments, participant, publication):
        for segment in segments:
            if segment.final:
                transcripts.append(segment.text)

    async def microphone():
        started = time.monotonic()
        offset = 0
        while True:
            if time.monotonic()-started > 5 and offset < len(pcm):
                chunk = pcm[offset:offset+960].ljust(960,b"\0")
                offset += 960
            else:
                chunk = bytes(960)
            await source.capture_frame(rtc.AudioFrame(chunk,24000,1,480))

    try:
        async with aiohttp.ClientSession(headers={"Authorization":"Bearer "+token}) as http:
            async with http.get("http://localhost:8811/api/voice/session") as r:
                r.raise_for_status()
                config = await r.json()
            await room.connect("ws://127.0.0.1:7880", config["token"])
            await room.local_participant.publish_track(rtc.LocalAudioTrack.create_audio_track("synthetic-test-mic",source),rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
            pump = asyncio.create_task(microphone())
            async with http.post("http://localhost:8811/api/voice/dispatch", json={"room":config["room"]}) as r:
                r.raise_for_status()
            await asyncio.sleep(27)
            assert received and any(received), "No returned audio"
    finally:
        if config:
            async with api.LiveKitAPI() as lk:
                await lk.room.delete_room(api.DeleteRoomRequest(room=config["room"]))
        if pump:
            pump.cancel()
            await asyncio.gather(pump,return_exceptions=True)
        await room.disconnect()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        await source.aclose()
    report = {"room":config["room"],"received_seconds":round(len(received)/48000,2),"transcripts":transcripts}
    (HERE/"output/phone-check.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps(report,indent=2))


if __name__ == "__main__":
    asyncio.run(main())
