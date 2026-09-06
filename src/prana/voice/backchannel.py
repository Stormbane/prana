"""Soft listening acks ("mm-hm") while Suti speaks at length.

ChatGPT's phone app backchannels mid-monologue; the Realtime API with
server VAD cannot — while the user is speaking, the model never emits
audio on its own. So the worker plays short pre-generated clips through
the SESSION'S OWN audio output (``session.say(text, audio=...)``): same
participant, same track, so the single-track invariant holds (a second
publishing participant SCTP-storms the box's SDK — the benched-music
lesson, 2026-09-01) and the box's AEC cancels the clip from the mic
like any other rendered audio. The say() text rides the existing
caption sink, so the karaoke shows the ack as it plays.

Clips live in ``~/.narada/voice/acks/*.wav`` (16-bit mono; the filename
is the karaoke text — ``mm-hm.wav`` shows as "mm-hm"). Generate them
with ``scripts/make_ack_clips.py``. No clips on disk = feature off.
"""

from __future__ import annotations

import logging
import random
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

ACKS_DIR = Path.home() / ".narada" / "voice" / "acks"


@dataclass
class AckClip:
    text: str
    pcm: bytes          # 16-bit mono little-endian
    sample_rate: int


def load_clips(directory: Path = ACKS_DIR) -> list[AckClip]:
    """All usable clips in the directory; unusable ones are logged and
    skipped, a missing directory is simply an empty list (feature off)."""
    clips: list[AckClip] = []
    try:
        paths = sorted(directory.glob("*.wav"))
    except OSError:
        return clips
    for p in paths:
        try:
            with wave.open(str(p), "rb") as w:
                if w.getnchannels() != 1 or w.getsampwidth() != 2:
                    logger.warning("ack clip %s skipped (need 16-bit "
                                   "mono WAV)", p.name)
                    continue
                clips.append(AckClip(
                    text=p.stem.replace("_", " "),
                    pcm=w.readframes(w.getnframes()),
                    sample_rate=w.getframerate()))
        except Exception as exc:
            logger.warning("ack clip %s unreadable: %s", p.name, exc)
    return clips


async def clip_frames(clip: AckClip, frame_ms: int = 20):
    """The clip as 20ms rtc.AudioFrames for session.say(audio=...)."""
    from livekit import rtc

    bytes_per_frame = clip.sample_rate * frame_ms // 1000 * 2
    pcm = clip.pcm
    if len(pcm) % bytes_per_frame:
        pcm += b"\x00" * (bytes_per_frame - len(pcm) % bytes_per_frame)
    for i in range(0, len(pcm), bytes_per_frame):
        chunk = pcm[i:i + bytes_per_frame]
        yield rtc.AudioFrame(data=chunk, sample_rate=clip.sample_rate,
                             num_channels=1,
                             samples_per_channel=len(chunk) // 2)


class BackchannelGate:
    """When to hum. Pure logic with injected time — testable without
    audio, VAD, or an event loop.

    An ack is due when the user has been speaking CONTINUOUSLY for
    ``min_speech_s`` and at least ``interval_s`` (+ jitter) has passed
    since the last ack. A pause resets the continuity clock — short
    exchanges never get hummed at; only real monologues do.
    """

    def __init__(self, min_speech_s: float = 8.0,
                 interval_s: float = 12.0, jitter_s: float = 4.0,
                 rng: Optional[random.Random] = None) -> None:
        self.min_speech_s = min_speech_s
        self.interval_s = interval_s
        self.jitter_s = jitter_s
        self._rng = rng or random.Random()
        self._speaking_since: Optional[float] = None
        self._next_ok_at = 0.0

    def on_user_state(self, speaking: bool, now: float) -> None:
        if speaking:
            if self._speaking_since is None:
                self._speaking_since = now
        else:
            self._speaking_since = None

    def should_ack(self, now: float) -> bool:
        return (self._speaking_since is not None
                and now - self._speaking_since >= self.min_speech_s
                and now >= self._next_ok_at)

    def mark_acked(self, now: float) -> None:
        self._next_ok_at = (now + self.interval_s
                            + self._rng.uniform(0.0, self.jitter_s))

    def pick(self, clips: list[AckClip]) -> AckClip:
        return self._rng.choice(clips)
