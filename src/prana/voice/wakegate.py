"""Wake gating — the realtime session opens only after "Narada".

Audio flows from the room continuously (LAN, free); this module watches
it with the locally-trained wake model and only then does the worker
connect the (billed) realtime session. Doubles as the first cost guard.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

WAKE_MODEL_PATH = Path(
    os.environ.get(
        "NARADA_WAKE_MODEL",
        str(Path.home() / ".narada" / "wakeword" / "output" / "narada"
            / "narada.onnx"),
    )
)

SAMPLE_RATE = 16000
CHUNK_SECONDS = 2.0
CHUNK_SAMPLES = int(SAMPLE_RATE * CHUNK_SECONDS)
HOP_SAMPLES = CHUNK_SAMPLES // 4  # 0.5 s hop


class WakeGate:
    """Sliding-window wake detection over a raw 16 kHz mono stream."""

    def __init__(self, threshold: float = 0.6,
                 model_path: Path = WAKE_MODEL_PATH) -> None:
        from livekit.wakeword import WakeWordModel  # heavy import, lazy

        if not model_path.is_file():
            raise FileNotFoundError(
                f"wake model missing: {model_path} — run "
                f"scripts/train-wakeword.ps1 first"
            )
        self._model = WakeWordModel(models=[str(model_path)])
        self._threshold = threshold
        self._buffer = np.zeros(0, dtype=np.float32)

    def feed(self, samples: np.ndarray) -> Optional[float]:
        """Feed 16 kHz mono float32 samples; returns confidence on wake."""
        self._buffer = np.concatenate([self._buffer, samples])
        while len(self._buffer) >= CHUNK_SAMPLES:
            chunk = self._buffer[:CHUNK_SAMPLES]
            self._buffer = self._buffer[HOP_SAMPLES:]
            scores = self._model.predict(chunk)
            for name, score in (scores or {}).items():
                if score >= self._threshold:
                    logger.info("wake: %s (%.2f)", name, score)
                    self._buffer = np.zeros(0, dtype=np.float32)
                    return float(score)
        return None
