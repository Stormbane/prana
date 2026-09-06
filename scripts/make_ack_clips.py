"""Generate the soft listening-ack clips for the backchannel feature.

Writes ~/.narada/voice/acks/*.wav — 16-bit mono, quiet by design, the
filename doubling as the karaoke text. Run once, and again only to
change the texts, voice, or gain:

    python scripts/make_ack_clips.py [--voice cedar] [--gain 0.45]

Uses OPENAI_API_KEY from ~/.narada/.voice.env (the same env the worker
loads). cedar/marin are realtime-only voices today — if the TTS
endpoint refuses the requested voice, falls back to "ash" (the closest
warm timbre) rather than failing the run.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import wave
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

ACKS = ["mm-hm", "hmm", "mm", "right"]
FALLBACK_VOICE = "ash"

TTS_INSTRUCTIONS = (
    "A soft, brief, breathy listening acknowledgement - barely voiced, "
    "low energy, like nodding along while someone else is talking. "
    "Under a second. Not a question, not enthusiastic - just presence."
)


def _synthesize(client, voice: str, text: str) -> bytes:
    resp = client.audio.speech.create(
        model="gpt-4o-mini-tts", voice=voice, input=text,
        instructions=TTS_INSTRUCTIONS, response_format="wav")
    return resp.read()


def _trim(samples: np.ndarray, sr: int, floor: int = 300,
          margin_s: float = 0.05) -> np.ndarray:
    """Cut the TTS padding — a second of leading silence would make the
    ack land after his sentence moved on."""
    loud = np.flatnonzero(np.abs(samples.astype(np.int32)) > floor)
    if loud.size == 0:
        return samples
    margin = int(sr * margin_s)
    lo = max(0, int(loud[0]) - margin)
    hi = min(len(samples), int(loud[-1]) + margin)
    return samples[lo:hi]


def main() -> int:
    load_dotenv(Path.home() / ".narada" / ".livekit.env")
    load_dotenv(Path.home() / ".narada" / ".voice.env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice",
                    default=os.environ.get("NARADA_VOICE_TIMBRE", "cedar"))
    ap.add_argument("--gain", type=float, default=0.45,
                    help="amplitude scale — acks sit under his voice")
    ap.add_argument("--out",
                    default=str(Path.home() / ".narada" / "voice" / "acks"))
    args = ap.parse_args()

    from openai import OpenAI
    client = OpenAI()
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    for text in ACKS:
        raw = None
        for voice in dict.fromkeys([args.voice, FALLBACK_VOICE]):
            try:
                raw = _synthesize(client, voice, text)
                if voice != args.voice:
                    print(f"  ({args.voice} refused — using {voice})")
                break
            except Exception as exc:
                err = exc
        if raw is None:
            print(f"FAILED {text}: {err}")
            return 1

        with wave.open(io.BytesIO(raw)) as w:
            sr, ch = w.getframerate(), w.getnchannels()
            samples = np.frombuffer(w.readframes(w.getnframes()),
                                    dtype=np.int16)
        if ch == 2:
            samples = samples.reshape(-1, 2).mean(axis=1).astype(np.int16)
        samples = _trim(samples, sr)
        samples = np.clip(samples.astype(np.float32) * args.gain,
                          -32768, 32767).astype(np.int16)

        path = outdir / f"{text}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(samples.tobytes())
        print(f"wrote {path} ({len(samples) / sr:.2f}s @ {sr}Hz)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
