"""Tap-admission protocol (M2 spec §2.2) — fail-closed by construction.

The BOX-3 asserts "a human tapped me" over the LiveKit data channel.
Because anyone in the room *could* send data packets, the assertion is
only trusted when ALL of these hold:

- sender participant identity == the device identity (``narada-box3``)
- it echoes the worker's **current, unused** cycle nonce (published at
  the start of each wake-watch cycle — one-shot, consumed on use)
- it arrives while the worker is actually waiting for admission

Anything else — wrong sender, stale/duplicate/absent nonce, malformed
payload — is ignored (never an error the model can observe). Tier
defaults to ``shareable``; ``personal`` must be explicit. The tier is
immutable for the life of the session it admits.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

DEVICE_IDENTITY = "narada-box3"
TIER_SHAREABLE = "shareable"
TIER_PERSONAL = "personal"
_VALID_TIERS = (TIER_SHAREABLE, TIER_PERSONAL)

TOPIC_ADMISSION = "narada.admission"   # worker → device: cycle nonce
TOPIC_TAP = "narada.tap"               # device → worker: tap assertion
TOPIC_SESSION = "narada.session"       # worker → device: session state


@dataclass(frozen=True)
class TapAssertion:
    tier: str
    nonce: str


class TapAdmission:
    """Nonce lifecycle + verification for one worker job."""

    def __init__(self, device_identity: str = DEVICE_IDENTITY) -> None:
        self._device = device_identity
        self._nonce: Optional[str] = None
        self._consumed = True  # nothing valid until new_cycle()

    def new_cycle(self) -> str:
        """Start a wake-watch cycle: mint a fresh one-shot nonce.
        Invalidates any prior nonce."""
        self._nonce = secrets.token_urlsafe(16)
        self._consumed = False
        return self._nonce

    def invalidate(self) -> None:
        """No assertion is acceptable until the next new_cycle()."""
        self._consumed = True

    def verify(self, sender_identity: Optional[str],
               payload: bytes | str) -> Optional[TapAssertion]:
        """Validate a data-channel packet. Returns the assertion on
        success (consuming the nonce), None otherwise. Never raises."""
        try:
            if sender_identity != self._device:
                if sender_identity is not None:
                    logger.warning(
                        "tap assertion from wrong identity %r — ignored",
                        sender_identity)
                return None
            if self._consumed or not self._nonce:
                logger.info("tap assertion outside an open cycle — ignored")
                return None
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8", errors="replace")
            msg = json.loads(payload)
            if not isinstance(msg, dict) or msg.get("type") != "tap":
                return None
            nonce = msg.get("nonce")
            if not isinstance(nonce, str) or not secrets.compare_digest(
                    nonce, self._nonce):
                logger.warning("tap assertion nonce mismatch — ignored")
                return None
            tier = msg.get("tier", TIER_SHAREABLE)
            if tier not in _VALID_TIERS:
                tier = TIER_SHAREABLE
            # one-shot: consume before returning
            self._consumed = True
            logger.info("tap admission verified (tier=%s)", tier)
            return TapAssertion(tier=tier, nonce=nonce)
        except Exception as exc:  # malformed anything → ignored
            logger.debug("tap assertion unparseable: %s", exc)
            return None


def is_sleep_tap(sender_identity: Optional[str],
                 payload: bytes | str) -> bool:
    """A tap arriving DURING a live session means 'stop'. Same sender
    check; no nonce needed (it can only end, never start, a session)."""
    try:
        if sender_identity != DEVICE_IDENTITY:
            return False
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", errors="replace")
        msg = json.loads(payload)
        # A tap carrying a nonce is a WAKE echo, not a stop: a fast
        # second tap lands before the box learns the session opened,
        # so it arrives wake-shaped — and must not end the session it
        # raced (field 2026-09-03: back-to-back taps killed newborn
        # sessions). Sleep taps are bare {"type": "tap"}.
        return (isinstance(msg, dict) and msg.get("type") == "tap"
                and "nonce" not in msg)
    except Exception:
        return False
