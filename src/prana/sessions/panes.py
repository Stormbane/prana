"""wezterm pane control — the human watch-and-take-over surface.

Owned sessions can be mirrored into wezterm panes; the registry stores
the pane id so ``focus_pane`` is pure UI focus, never a privilege change.

Hard-won rule: ``wezterm cli`` BLOCKS trying to auto-start a GUI mux if
none is running. Every call here passes ``--no-auto-start`` where
supported and carries a timeout; absence of wezterm (or of a running
mux) degrades to "no panes", never to a hang.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from typing import Optional

from prana.spawn import run_hidden

logger = logging.getLogger(__name__)

_TIMEOUT_S = 5.0


def _wezterm() -> Optional[str]:
    return shutil.which("wezterm")


def _cli(*args: str, timeout: float = _TIMEOUT_S) -> Optional[str]:
    exe = _wezterm()
    if exe is None:
        return None
    try:
        result = run_hidden(
            [exe, "cli", "--no-auto-start", *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning("wezterm cli %s timed out", args[:1])
        return None
    if result.returncode != 0:
        logger.debug("wezterm cli %s failed: %s", args[:1], result.stderr.strip())
        return None
    return result.stdout


def available() -> bool:
    """True if wezterm is installed AND a mux is answering."""
    return _cli("list", "--format", "json") is not None


def list_pane_ids() -> list[str]:
    out = _cli("list", "--format", "json")
    if out is None:
        return []
    try:
        return [str(p["pane_id"]) for p in json.loads(out)]
    except (ValueError, KeyError, TypeError):
        logger.warning("wezterm list output unparseable")
        return []


def spawn_pane(cwd: str, argv: list[str]) -> Optional[str]:
    """Open a new wezterm window/pane running argv; return its pane id.

    Uses ``wezterm cli spawn --new-window`` when a mux is up; falls back
    to ``wezterm start`` (which starts the GUI) when none is running.
    Returns None if wezterm is unavailable — callers treat panes as
    optional garnish, never a spawn prerequisite.
    """
    out = _cli("spawn", "--new-window", "--cwd", cwd, "--", *argv)
    if out is not None:
        pane_id = out.strip()
        return pane_id or None
    exe = _wezterm()
    if exe is None:
        return None
    try:
        run_hidden(
            [exe, "start", "--cwd", cwd, "--", *argv],
            timeout=_TIMEOUT_S, capture_output=True,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("wezterm start failed: %s", exc)
        return None
    # `wezterm start` doesn't report a pane id; pick up the newest pane.
    ids = list_pane_ids()
    return ids[-1] if ids else None


def get_text(pane_id: str, lines: int = 200) -> Optional[str]:
    return _cli("get-text", "--pane-id", pane_id,
                "--start-line", str(-abs(lines)))


def send_text(pane_id: str, text: str) -> bool:
    return _cli("send-text", "--pane-id", pane_id, "--no-paste", text) is not None


def focus_pane(pane_id: str) -> bool:
    return _cli("activate-pane", "--pane-id", pane_id) is not None
