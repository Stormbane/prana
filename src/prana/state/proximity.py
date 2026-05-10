"""Proximity detection — is Suti at his PC right now?

Windows-only for now. Uses GetLastInputInfo to read the time since the
last keyboard or mouse input; if it's below a threshold, the user is
considered present at the PC.

Linux/macOS: returns "at PC" optimistically (i.e. always True) — if you
need real proximity on those platforms, plug in xprintidle / ioreg here.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

logger = logging.getLogger(__name__)


DEFAULT_IDLE_THRESHOLD_S = 120


if sys.platform == "win32":

    class _LASTINPUTINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("dwTime", wintypes.DWORD),
        ]

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32

    def idle_seconds() -> float:
        """Return seconds since the last keyboard/mouse input.

        GetLastInputInfo returns a tick count (ms) of the last input
        event, ignoring focus-stealing screen-saver / RDP / lock states.
        We compare against the system tick count (which wraps every ~49
        days; the subtraction handles wrap correctly because both are
        DWORD-modular).
        """
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not _user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        now = _kernel32.GetTickCount()
        # DWORD modular subtraction
        delta_ms = (now - info.dwTime) & 0xFFFFFFFF
        return delta_ms / 1000.0

else:

    def idle_seconds() -> float:
        # Non-Windows: we don't have a uniform proximity API yet.
        # Return 0 so is_at_pc() defaults to True. Replace with
        # platform-specific logic if you ever need it.
        return 0.0


def is_at_pc(threshold_s: float = DEFAULT_IDLE_THRESHOLD_S) -> bool:
    """True if last input was within `threshold_s` seconds.

    The 120s default is intentionally generous — Suti might be reading
    code, watching a build, or thinking. We don't want to push to phone
    every time he stops typing for 30 seconds.
    """
    idle = idle_seconds()
    at_pc = idle <= threshold_s
    logger.debug("proximity: idle=%.1fs threshold=%.1fs at_pc=%s",
                 idle, threshold_s, at_pc)
    return at_pc
