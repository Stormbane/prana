"""Voice prefs — ONE reader/writer for ~/.narada/brain/voice-prefs.json.

Two processes touch this file: the brain (Settings → POST
/api/voice/prefs) and the phone worker (the brief_mode voice tool).
Independent read-modify-write lost updates and a truncating write left
invalid JSON that readers silently took for defaults (review
2026-09-08). Here: an interprocess lock around the read-modify-write,
an atomic replace from a temp file, and a malformed file is an ERROR
for writers (never clobbered), an empty dict for readers.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


def prefs_path() -> Path:
    return Path.home() / ".narada" / "brain" / "voice-prefs.json"


class PrefsError(RuntimeError):
    """The prefs file exists but is not a JSON object — refuse to write
    over it; a human should look."""


def _load(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise PrefsError(f"{path} is not a JSON object")
    return data


def read_prefs(path: Path | None = None) -> dict:
    """Current prefs; {} when absent or unreadable (readers degrade)."""
    path = path or prefs_path()
    try:
        return _load(path)
    except (OSError, ValueError, PrefsError):
        return {}


class _Lock:
    """A lock FILE beside the prefs (O_EXCL create), portable across
    Windows and POSIX; stale after 10s so a crashed writer can't wedge
    Settings forever."""

    def __init__(self, target: Path, timeout_s: float = 5.0, stale_s: float = 10.0):
        self.path = target.with_suffix(target.suffix + ".lock")
        self.timeout_s = timeout_s
        self.stale_s = stale_s
        self._fd: int | None = None

    def __enter__(self) -> "_Lock":
        deadline = time.monotonic() + self.timeout_s
        while True:
            try:
                self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > self.stale_s:
                        self.path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise PrefsError("voice prefs are locked by another writer") from None
                time.sleep(0.05)

    def __exit__(self, *_exc: object) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


def update_prefs(patch: dict, path: Path | None = None) -> dict:
    """Merge ``patch`` into the prefs under the lock; write atomically;
    return the merged prefs. Raises PrefsError on a malformed existing
    file or a held lock."""
    path = path or prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _Lock(path):
        try:
            current = _load(path)
        except FileNotFoundError:
            current = {}
        except (ValueError, PrefsError) as exc:
            raise PrefsError(f"voice prefs unreadable, not overwriting: {exc}") from exc
        merged = {**current, **patch}
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(merged, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    return merged
