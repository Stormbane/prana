"""Presence — is Suti at his desk right now?

The body is the primary sensor for proprioception. deha exposes
``GET /presence`` (see deha/docs/contracts/presence.md) returning a
fused signal from radar, camera, and microphone. We poll it; if the
endpoint is unreachable (deha down, endpoint not yet implemented, or
network glitch), we fall back to PC-input idle detection from
:mod:`prana.state.proximity`.

The PC input remains a useful *secondary* signal — Suti might be at the
PC reading code with hands not moving, while the body's radar is in a
different room and reports no presence. is_present() therefore returns
True if EITHER body or PC says yes:

    is_present()  =  body_sees_someone  OR  pc_input_recent

A 5-second in-process cache dampens the cost of rapid successive
checks (e.g. several utterances queued back-to-back). Set
``PRANA_PRESENCE_CACHE_S=0`` in the env to disable caching.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request

from prana.state.proximity import is_at_pc as is_pc_active
from prana.state.proximity import idle_seconds as pc_idle_seconds

logger = logging.getLogger(__name__)


DEHA_PRESENCE_URL = os.environ.get(
    "DEHA_PRESENCE_URL", "http://127.0.0.1:8765/presence"
)
DEHA_PRESENCE_TIMEOUT_S = float(os.environ.get("DEHA_PRESENCE_TIMEOUT_S", "1.0"))
PRESENCE_CACHE_S = float(os.environ.get("PRANA_PRESENCE_CACHE_S", "5.0"))


# In-process cache: (timestamp, presence_dict_or_None)
_cache: tuple[float, dict | None] | None = None


def _query_deha_presence() -> dict | None:
    """One poll of deha's /presence endpoint. Returns the parsed dict on
    200 OK with a body-present field, or None on any failure."""
    try:
        with urllib.request.urlopen(
            DEHA_PRESENCE_URL, timeout=DEHA_PRESENCE_TIMEOUT_S
        ) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as exc:
        logger.debug("deha /presence unreachable: %s — falling back to PC input", exc)
        return None

    if not isinstance(data, dict) or "present" not in data:
        logger.debug("deha /presence returned unexpected payload: %r", data)
        return None
    return data


def _get_body_presence() -> dict | None:
    """Cached deha presence query. Returns None if endpoint unavailable."""
    global _cache
    now = time.monotonic()
    if _cache and PRESENCE_CACHE_S > 0:
        cached_at, cached_value = _cache
        if (now - cached_at) <= PRESENCE_CACHE_S:
            return cached_value

    fresh = _query_deha_presence()
    _cache = (now, fresh)
    return fresh


def body_sees_someone() -> bool:
    """True iff deha /presence is reachable AND reports present=true.

    Returns False on any failure (endpoint down, parse error, etc.) —
    callers should combine with is_pc_active() so a body-side outage
    doesn't make Narada think Suti is gone."""
    presence = _get_body_presence()
    return bool(presence and presence.get("present"))


def is_present(pc_idle_threshold_s: float = 120.0) -> bool:
    """Combined presence signal. True if EITHER body or PC says present.

    Returns True when:
    - deha /presence reports present=true, OR
    - PC input was within ``pc_idle_threshold_s`` seconds (default 2 min)

    This makes the system tolerant to single-source failure:
    - body radar broken? PC keystrokes still register presence
    - laptop closed? body still sees Suti at his desk
    """
    if body_sees_someone():
        return True
    return is_pc_active(threshold_s=pc_idle_threshold_s)


def presence_snapshot() -> dict:
    """Diagnostic snapshot of all presence signals — for /status, logs,
    and smoke tests. Doesn't combine; reports each source raw."""
    body = _get_body_presence()
    return {
        "present": is_present(),
        "body": {
            "available": body is not None,
            "raw": body,
        },
        "pc": {
            "active": is_pc_active(),
            "idle_seconds": pc_idle_seconds(),
        },
    }
