"""The Gemini adapter moved into prana (W5a, 2026-11-05). This name stays so
the lab's scripts and tests keep working; it IS prana.voice.gemini.adapter,
with the lab's own output/ for summaries and the closing clip."""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ.setdefault("NARADA_GEMINI_OUTPUT", str(HERE / "output"))
sys.path.insert(0, str(HERE.parents[1] / "src"))
from prana.voice.gemini import adapter as _adapter  # noqa: E402

sys.modules[__name__] = _adapter
