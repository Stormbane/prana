"""TV mode — a hearing profile for noisy rooms (Suti, 2026-09-03).

The turn detector is an energy gate; TV dialogue is human speech at
speaking volume, so to the model the TV is a person in the room — it
barges in on Narada and even prompts replies. Until the voice can
tell WHO is speaking (speaker enrollment, a future project), TV mode
trades barge-in for immunity: voice interruptions off (tap still
stops him) and the speech gate raised above TV-level audio.

Persisted as a flag file so it survives the one-session-per-job
recycle; each session reads it at build time.
"""

from pathlib import Path

FLAG = Path.home() / ".narada" / "voice-tv-mode.flag"


def set_tv_mode(on: bool) -> None:
    if on:
        FLAG.write_text("on", encoding="utf-8")
    else:
        FLAG.unlink(missing_ok=True)


def tv_mode_on() -> bool:
    return FLAG.exists()
