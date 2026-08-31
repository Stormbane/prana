"""Standalone music publisher — radio in a blast-proof box.

Field incident 2026-08-31 23:42:54: once the box actually subscribed to
the music track, the worker process died silently ~29s into playback —
a native crash in the rtc publish path, unreachable from Python. The
answer is isolation, not a patch: this module runs as ITS OWN process
with its own room participant. If it dies, music stops and nothing else
notices; the worker (the tap, the sessions, the watchdogs) cannot be
collateral damage.

Contract with the owning worker (prana.voice.music.MusicPlayer):
- argv: station name, stream url, initial volume (0-100)
- stdin: "vol <0-100>\\n" adjusts gain live; EOF means the worker died
  or wants us gone -> exit. Being killed is also a normal ending.
- The worker enforces the one-track invariant by never running this
  process during an admitted session.

Publishes as SOURCE_MICROPHONE (the box's SDK renders only that) under
its own identity, token minted from the server keys.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("narada-music-pub")

SAMPLE_RATE = 48000
CHANNELS = 2
FRAME_SAMPLES = SAMPLE_RATE // 100  # 10 ms

ROOM = os.environ.get("NARADA_VOICE_ROOM", "narada-body")
IDENTITY = "narada-music"

volume = 60


def _mint_token() -> str:
    from livekit import api as lkapi
    return (lkapi.AccessToken()
            .with_identity(IDENTITY)
            .with_name("Narada's radio")
            .with_grants(lkapi.VideoGrants(
                room_join=True, room=ROOM,
                can_publish=True, can_subscribe=False,
                can_publish_data=False))
            .to_jwt())


async def _watch_stdin() -> None:
    """Volume lines in; EOF = the worker is gone, so are we."""
    global volume
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            logger.info("stdin closed — exiting with the worker")
            os._exit(0)
        parts = line.strip().split()
        if len(parts) == 2 and parts[0] == "vol":
            try:
                volume = max(0, min(100, int(parts[1])))
                logger.info("volume -> %d", volume)
            except ValueError:
                pass


async def main() -> int:
    global volume
    name, url = sys.argv[1], sys.argv[2]
    volume = max(0, min(100, int(sys.argv[3]))) if len(sys.argv) > 3 else 60

    from livekit import rtc

    room = rtc.Room()
    await room.connect(os.environ["LIVEKIT_URL"], _mint_token())
    source = rtc.AudioSource(SAMPLE_RATE, CHANNELS)
    track = rtc.LocalAudioTrack.create_audio_track("narada-music", source)
    await room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(
            source=rtc.TrackSource.SOURCE_MICROPHONE))
    logger.info("publishing %s (%s)", name, url)

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", url, "-vn",
        "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS), "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    asyncio.ensure_future(_watch_stdin())

    bytes_per_frame = FRAME_SAMPLES * CHANNELS * 2
    try:
        while True:
            data = await proc.stdout.readexactly(bytes_per_frame)
            if volume < 100:
                samples = np.frombuffer(data, dtype=np.int16)
                scaled = (samples.astype(np.int32) * volume) // 100
                data = np.clip(scaled, -32768, 32767).astype(np.int16).tobytes()
            await source.capture_frame(rtc.AudioFrame(
                data=data, sample_rate=SAMPLE_RATE,
                num_channels=CHANNELS,
                samples_per_channel=FRAME_SAMPLES))
    except asyncio.IncompleteReadError:
        logger.info("stream ended")
        return 2
    finally:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(room.disconnect(), timeout=3.0)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    load_dotenv(Path.home() / ".narada" / ".livekit.env")
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
