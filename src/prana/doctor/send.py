"""The one place the doctor speaks: the once-daily red digest.

Everything else in prana.doctor only reads. This module sends, and only
through smriti's single notify point (`~/.narada/.smriti/hooks/notify_suti.py`,
the script the `tell_suti` MCP tool wraps), so delivery, channel
routing and the System log record are the same as any other fault.

No external dead-man service (Suti, 2026-11-04: no extra accounts). The
morning message is the heartbeat he notices; this digest names what is
red. If the PC is off, neither arrives, and the missing morning message
is the signal.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from prana.doctor.digest import compose_digest, digest_due, fingerprint
from prana.doctor.model import Report

NOTIFY_TIMEOUT_S = 90


def notify_script(narada: Path) -> Path:
    return narada / ".smriti" / "hooks" / "notify_suti.py"


def load_state(path: Path) -> dict | None:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def digest_text(report: Report, local_tz, state: dict | None, today: date) -> str | None:
    """The digest, marked as a repeat when the same checks were red for
    the same reasons yesterday."""
    text = compose_digest(report, local_tz)
    if text is None:
        return None
    fp = fingerprint(report)
    if state and state.get("fingerprint") == fp and \
            state.get("day") == (today - timedelta(days=1)).isoformat():
        streak = int(state.get("streak", 1)) + 1
        text = f"(Unchanged, day {streak}.)\n" + text
    return text


def send_digest(report: Report, local_tz, state_path: Path, narada: Path,
                today: date, run=subprocess.run) -> tuple[int, str]:
    """Send today's digest if one is due. Returns (exit code, note):
    0 sent or nothing due, 1 delivery failed (the state is then left
    alone, so the next run tries again)."""
    state = load_state(state_path)
    if not digest_due(report, state, today):
        return 0, "no digest due (nothing red, or already sent today)"
    text = digest_text(report, local_tz, state, today)
    if text is None:
        return 0, "nothing red"
    fp = fingerprint(report)
    # PYTHONUTF8: the child reads stdin in the locale code page (cp1252)
    # otherwise, and the digest carries non-ASCII (→, …).
    env = dict(os.environ, PYTHONUTF8="1", NOTIFY_LEVEL="error", NOTIFY_SOURCE="doctor",
               NOTIFY_CATEGORY="doctor", NOTIFY_EVENT_ID=f"doctor-{today.isoformat()}-{fp}")
    try:
        res = run([sys.executable, str(notify_script(narada))], input=text, text=True,
                  encoding="utf-8", capture_output=True, env=env, timeout=NOTIFY_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"notify_suti did not run: {type(exc).__name__}"
    if res.returncode != 0:
        return 1, f"notify_suti exited {res.returncode}: {(res.stderr or '').strip()[-200:]}"
    streak = 1
    if state and state.get("fingerprint") == fp and \
            state.get("day") == (today - timedelta(days=1)).isoformat():
        streak = int(state.get("streak", 1)) + 1
    _save_state(state_path, {"day": today.isoformat(), "fingerprint": fp, "streak": streak,
                             "delivered": (res.stderr or "").strip().splitlines()[-1:]})
    return 0, "digest sent"
