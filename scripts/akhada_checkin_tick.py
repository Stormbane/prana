"""Hermes-fired Akhada check-in tick — one nudge decision, then exit.

Wired as a Hermes `--no-agent --script` cron job at the ruled slots
(11:00, 18:00, 22:00 Brisbane; intake 2026-09-04). The script IS the
job: akhada decides deterministically whether a nudge is worth sending
(due desires, day standing, one-per-day-slot dedupe), the line goes
through the brain's notify door (the app's push, with Telegram only as
its fallback; 2026-11-04), or through prana's router (body if Suti is
present, else Telegram) when the brain can't be reached at all, and the
sent nudge is recorded back into akhada's store. No LLM anywhere in this
path — the conversation happens in the session the link opens.

Exit 0 always unless the machinery itself broke: "stayed quiet" is a
success, not a failure.

Delivery semantics are AT-MOST-ONCE, by claim-before-send on a unique
(day, slot) index: a duplicate nudge erodes trust; a lost one
self-heals at the next slot. Accepted residual (Codex review): a
"body" delivery is deha's enqueue acknowledgement, not confirmed
playback — if deha dies before speaking, that slot's nudge is lost.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

QUIET_START_H, QUIET_END_H = 4, 7  # ruled: 04:00-07:00 Brisbane
TALK_URL = os.environ.get(
    "AKHADA_TALK_URL",
    "https://<machine>.<tailnet>.ts.net:8799/talk")


def _talk_page_up() -> bool:
    """Only offer the link when the dashboard actually serves /talk —
    a nudge pointing at a dead page is worse than a plain nudge (the
    dashboard component and T1's HTTPS both land later than this tick)."""
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:8799/talk",
                                    timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


BRAIN_URL = os.environ.get("NARADA_BRAIN_URL", "http://127.0.0.1:8811")
BRAIN_TOKENS = Path.home() / ".narada" / ".brain-tokens.json"


class DoorUnreachable(RuntimeError):
    """The brain's notify door could not be reached at all: nothing was
    sent, so another route may carry the nudge without a duplicate."""


def _door(text: str, event_id: str) -> dict:
    """Send a nudge through the brain's notify door, the one route every
    sender uses (2026-11-04). The door pushes to the app and falls back to
    Telegram only when no device got the push (brain/push.py), so a
    check-in no longer reaches Telegram as well as the app.

    Raises DoorUnreachable when nothing could have been sent (no token,
    connection refused). Any other failure (a timeout after the request
    went out) is ambiguous, and propagates."""
    import json
    import urllib.error
    import urllib.request
    try:
        token = json.loads(BRAIN_TOKENS.read_text(encoding="utf-8"))["prana"]
    except (OSError, ValueError, KeyError) as exc:
        raise DoorUnreachable(f"no prana token ({type(exc).__name__})") from None
    body = json.dumps({
        "title": "Narada", "body": text, "level": "message",
        "source": "akhada-checkin", "category": "akhada-checkin",
        "event_id": event_id, "url": "./#/today", "delivered_channels": [],
    }).encode("utf-8")
    req = urllib.request.Request(
        BRAIN_URL + "/v1/narada/notify", data=body, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ConnectionRefusedError):
            raise DoorUnreachable("brain not running (connection refused)") from None
        raise


def _notify_app(text: str, event_id: str, telegram_done: bool) -> str:
    """Mirror a delivered nudge into the brain's notify door, so it lands
    in the app's feed and on the phone as a push (2026-10-07: nudges went
    only to the box or Telegram and never reached the app). Best effort
    and after delivery: the at-most-once claim above is already settled,
    so a brain wobble here costs a push, never a duplicate Telegram (the
    door is told Telegram is done) and never a second app row (event_id).
    """
    import json
    import urllib.request
    try:
        token = json.loads(BRAIN_TOKENS.read_text(encoding="utf-8"))["prana"]
        body = json.dumps({
            "title": "Narada", "body": text, "level": "message",
            "source": "akhada-checkin", "category": "akhada-checkin",
            "event_id": event_id, "url": "./#/today",
            "delivered_channels": ["telegram"] if telegram_done else [],
        }).encode("utf-8")
        req = urllib.request.Request(
            BRAIN_URL + "/v1/narada/notify", data=body, method="POST",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read().decode("utf-8") or "{}")
        return f"app: channels={result.get('channels')} push={result.get('push')}"
    except Exception as exc:  # noqa: BLE001 — the nudge already went out
        return f"app: not mirrored ({exc})"


def main() -> int:
    now = datetime.now()
    if QUIET_START_H <= now.hour < QUIET_END_H:
        print(f"quiet hours ({now:%H:%M}) — no nudge")
        return 0

    from akhada.checkin import (build_checkin, claim_checkin,
                                confirm_checkin, mark_sending,
                                release_checkin)
    from akhada.store.db import Store

    store = Store()
    out = build_checkin(store)
    if not out["due"]:
        print(f"stayed quiet: {out['reason']}")
        return 0

    # Claim the slot BEFORE sending (unique index = the lock): two
    # overlapping ticks cannot both nudge. At-most-once on purpose — a
    # duplicate nudge costs trust, a lost one self-heals next slot.
    if not claim_checkin(store, out["slot"], out["day"]):
        print(f"slot {out['day']}/{out['slot']} already claimed — quiet")
        return 0

    # Phase 1 — delivery has NOT begun: any failure here releases the
    # claim (a hard kill leaves a 'claimed' row the stale breaker in
    # claim_checkin can re-take).
    try:
        text = out["text"]
        if _talk_page_up():
            text += f"\nTalk: {TALK_URL}"
        from prana.state.router import route_utterance
    except BaseException:
        release_checkin(store, out["slot"], out["day"])
        raise

    # Phase 2 — delivery may happen from here on: an exception is
    # AMBIGUOUS (the router may have sent before raising), so the
    # claim is deliberately KEPT — at-most-once resolves ambiguity
    # toward a lost slot, never a duplicate. Only a clean "nothing
    # was delivered" result releases.
    mark_sending(store, out["slot"], out["day"])
    event_id = f"akhada-checkin:{out['day']}:{out['slot']}"
    try:
        door = _door(text, event_id)
    except DoorUnreachable as exc:
        # Nothing went out: the old direct route (body if Suti is there,
        # else Telegram) carries it, then mirrors into the app when it can.
        print(f"brain door unreachable ({exc}); routing directly")
        result = route_utterance(text, source="akhada-checkin", topic="fitness")
        if not result.ok:
            release_checkin(store, out["slot"], out["day"])
            print(f"nudge NOT delivered (claim released): "
                  f"body={result.body_error} telegram={result.telegram_error}")
            return 1
        confirm_checkin(store, out["slot"], out["day"], result.delivered_to, out["text"])
        print(f"nudge sent via {result.delivered_to} [{out['slot']}]: {out['text']}")
        print(_notify_app(out["text"], event_id,
                          telegram_done="telegram" in str(result.delivered_to)))
        return 0
    except Exception as exc:  # noqa: BLE001 — ambiguous: the claim is kept
        print(f"nudge delivery uncertain, claim kept (at most once): {exc}")
        return 1

    delivered = []
    if int((door.get("push") or {}).get("sent", 0)) > 0:
        delivered.append("app")
    if door.get("telegram") is True:
        delivered.append("telegram")
    if not delivered:
        release_checkin(store, out["slot"], out["day"])
        print(f"nudge NOT delivered (claim released): channels={door.get('channels')} "
              f"push={door.get('push')} telegram={door.get('telegram')}")
        return 1
    confirm_checkin(store, out["slot"], out["day"], "+".join(delivered), out["text"])
    print(f"nudge sent via {'+'.join(delivered)} [{out['slot']}]: {out['text']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
