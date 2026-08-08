"""Measure the wake model's FALSE-ACCEPT rate (cross-review #4).

Recall (2/30 on Suti's voice) measures false negatives; the cost/privacy
risk is false *accepts* — ambient audio or near-phrases opening a billed,
transcribing session. This feeds non-"Narada" audio through the same
WakeGate the worker uses and counts spurious triggers.

Sources: the 774 real background-noise clips from training data (ambient
room-ish audio) + TTS-synthesized adversarial near-phrases (Nevada,
Ramada, ...). Honest limitation: not *this* room's mic yet — that needs
the BOX-3 as a capture source (Phase 3). This is the best headless proxy.

    python scripts/eval_wake_false_accept.py            # thresholds 0.5 + 0.32
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np

BG_DIR = Path.home() / ".narada" / "wakeword" / "data" / "backgrounds"
NEAR_PHRASES = ["Nevada", "Ramada", "Granada", "Armada", "not rather",
                "no radar", "narrator", "enchilada", "the data", "banana",
                "we're not ready", "narrate this", "in Nevada now"]
THRESHOLDS = [0.5, 0.32]


def _load_16k_mono(path: Path) -> np.ndarray:
    import librosa
    y, _ = librosa.load(str(path), sr=16000, mono=True)
    return y.astype(np.float32)


def _count_triggers(model, audio: np.ndarray, threshold: float) -> int:
    """Slide 2s windows (0.5s hop) — same as WakeGate — count triggers."""
    win, hop = 32000, 8000
    triggers, i = 0, 0
    while i + win <= len(audio):
        scores = model.predict(audio[i:i + win])
        if any(s >= threshold for s in (scores or {}).values()):
            triggers += 1
            i += win  # debounce: skip the whole window after a trigger
        else:
            i += hop
    return triggers


def main() -> None:
    from livekit.wakeword import WakeWordModel

    model_path = (Path.home() / ".narada" / "wakeword" / "output" / "narada"
                  / "narada.onnx")
    if not model_path.is_file():
        sys.exit(f"wake model missing: {model_path}")
    model = WakeWordModel(models=[str(model_path)])

    # 1. Ambient background audio (recursive — clips live in subdirs)
    bg_files = sorted(BG_DIR.rglob("*.wav"))[:400]  # cap for runtime
    print(f"[bg] feeding {len(bg_files)} background clips...")
    bg_audio = []
    total_s = 0.0
    for f in bg_files:
        try:
            y = _load_16k_mono(f)
        except Exception:
            continue
        bg_audio.append(y)
        total_s += len(y) / 16000
    bg = np.concatenate(bg_audio) if bg_audio else np.zeros(0, np.float32)
    hours = total_s / 3600
    print(f"[bg] {total_s:.0f}s ({hours:.2f}h) of ambient audio\n")

    for th in THRESHOLDS:
        trig = _count_triggers(model, bg, th)
        fpph = trig / hours if hours else 0
        print(f"[bg] threshold {th}: {trig} false accepts  ->  "
              f"{fpph:.2f} per hour")

    # 2. Adversarial near-phrases via TTS
    print(f"\n[near] synthesizing {len(NEAR_PHRASES)} near-phrases...")
    import requests
    key = os.environ.get("OPENAI_API_KEY", "")
    for th in THRESHOLDS:
        false_accepts = 0
        for phrase in NEAR_PHRASES:
            try:
                r = requests.post(
                    "https://api.openai.com/v1/audio/speech",
                    headers={"Authorization": f"Bearer {key}"},
                    json={"model": "gpt-4o-mini-tts", "voice": "alloy",
                          "input": phrase, "response_format": "pcm"},
                    timeout=30)
                pcm = np.frombuffer(r.content, dtype=np.int16).astype(np.float32) / 32768.0
                # resample 24k->16k
                import librosa
                y = librosa.resample(pcm, orig_sr=24000, target_sr=16000)
                if _count_triggers(model, y, th) > 0:
                    false_accepts += 1
                    print(f"[near] threshold {th}: FALSE ACCEPT on {phrase!r}")
            except Exception as exc:
                print(f"[near] {phrase!r} failed: {exc}")
        print(f"[near] threshold {th}: {false_accepts}/{len(NEAR_PHRASES)} "
              f"near-phrases falsely accepted")


if __name__ == "__main__":
    main()
