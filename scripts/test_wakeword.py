"""Live mic test for the Narada wake word — no API key, no LiveKit.

Say "Narada" at your PC mic; each detection prints with its confidence.
Walk around, vary your distance and tone, try to trip it with near-miss
words. This tells us whether the synthetic-voice model is already good
enough or whether it's worth retraining on real recordings.

    python scripts/test_wakeword.py                 # threshold 0.5
    python scripts/test_wakeword.py --threshold 0.32  # trainer's optimum

Ctrl-C to stop.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

MODEL = Path(
    os.environ.get(
        "NARADA_WAKE_MODEL",
        str(Path.home() / ".narada" / "wakeword" / "output" / "narada"
            / "narada.onnx"),
    )
)


async def main(threshold: float) -> None:
    from livekit.wakeword import WakeWordListener, WakeWordModel

    if not MODEL.is_file():
        raise SystemExit(f"model not found: {MODEL} — train it first")

    model = WakeWordModel(models=[str(MODEL)])
    print(f"listening for 'Narada' (threshold={threshold}) — say it now, "
          f"Ctrl-C to stop\n")
    count = 0
    async with WakeWordListener(model, threshold=threshold, debounce=1.5) as listener:
        while True:
            det = await listener.wait_for_detection()
            count += 1
            print(f"  #{count}  DETECTED {det.name!r}  "
                  f"confidence={det.confidence:.2f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=0.5,
                    help="detection threshold 0-1 (lower = more sensitive)")
    args = ap.parse_args()
    try:
        asyncio.run(main(args.threshold))
    except KeyboardInterrupt:
        print("\nstopped.")
