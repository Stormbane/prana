"""Render reusable voice auditions through the production speech pipeline.

Run in .venv-voice; this makes a bounded paid provider call per missing voice.
No microphone, rooms, tools, personal context, transcripts or saved-pref writes.
"""
import argparse
import asyncio
import json
import time
import wave
from pathlib import Path

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents.voice import io

from prana.voice.pipeline import VoicePipeline
from prana.voice.prefs import VOICE_OPTIONS

WORDS = "Hello, I'm Narada. There you are. Shall we follow that thought a little further? The tea can wait; a good question rarely does."


class Silence(io.AudioInput):
    def __init__(self):
        super().__init__(label="Preview silence; no microphone")

    async def __anext__(self):
        await asyncio.sleep(.02)
        return rtc.AudioFrame(data=bytes(960), sample_rate=24000,
                              num_channels=1, samples_per_channel=480)


class Recording(io.AudioOutput):
    def __init__(self):
        super().__init__(label="Preview recording", capabilities=io.AudioOutputCapabilities(pause=False))
        self.frames = []
        self.rate = 24000
        self.segment_seconds = 0
        self.done = asyncio.Event()

    async def capture_frame(self, frame):
        await super().capture_frame(frame)
        self.rate = frame.sample_rate
        self.frames.append(bytes(frame.data))
        self.segment_seconds += frame.samples_per_channel / frame.sample_rate

    def flush(self):
        super().flush()
        self.on_playback_finished(playback_position=self.segment_seconds, interrupted=False)
        if self.segment_seconds > 1:
            self.done.set()
        self.segment_seconds = 0

    def clear_buffer(self):
        self.on_playback_finished(playback_position=self.segment_seconds, interrupted=True)
        self.segment_seconds = 0


async def render(voice, output):
    target = output / f"{voice}.wav"
    if target.exists():
        return
    pipeline = VoicePipeline(surface="preview", prefs={"model": "gpt-live-1", "live_voice": voice})
    sink = Recording()
    pipeline.session.input.audio = Silence()
    pipeline.session.output.audio = sink
    started = time.monotonic()
    try:
        async with asyncio.timeout(25):
            agent = pipeline.agent(instructions="Voice audition only. No tools or personal context.", tools=[])
            await pipeline.session.start(agent=agent)
            pipeline.session.generate_reply(instructions=f"For this voice audition, say only: {WORDS}")
            await sink.done.wait()
    finally:
        await pipeline.aclose()
    pcm = b"".join(sink.frames)
    if len(pcm) < sink.rate * 2 or not any(pcm):
        raise RuntimeError(f"No usable audio for {voice}")
    output.mkdir(parents=True, exist_ok=True)
    with wave.open(str(target), "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(sink.rate); wav.writeframes(pcm)
    report = {"model": "gpt-live-1", "voice": voice, "prompt": WORDS,
              "audioSeconds": len(pcm) / (sink.rate * 2),
              "wallSeconds": round(time.monotonic()-started, 2), "usage": pipeline.provider_usage}
    target.with_suffix('.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(f"{voice}: {report['audioSeconds']:.1f}s audio", flush=True)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('voices', nargs='*')
    args = parser.parse_args()
    allowed = [v['id'] for v in VOICE_OPTIONS['live_voice']]
    chosen = args.voices or allowed
    if any(v not in allowed for v in chosen):
        parser.error('Unsupported voice')
    load_dotenv(Path.home()/'.narada/.voice.env')
    for voice in chosen:
        await render(voice, args.output)


if __name__ == '__main__':
    asyncio.run(main())
