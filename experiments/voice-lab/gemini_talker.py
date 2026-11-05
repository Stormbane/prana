"""The talker core moved into prana (W5a, 2026-11-05): one cost guard for the
lab and the worker. This name stays so the lab's scripts keep working; it IS
prana.voice.gemini.core (private names and monkeypatches included)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from prana.voice.gemini import core as _core  # noqa: E402

sys.modules[__name__] = _core
