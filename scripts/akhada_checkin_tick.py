"""Hermes-fired Akhada check-in tick — one nudge decision, then exit.

Wired as a Hermes `--no-agent --script` cron job at the ruled slots
(11:00, 18:00, 22:00 Brisbane; intake 2026-09-04). The script IS the
job: akhada decides deterministically whether a nudge is worth sending
(due desires, day standing, one-per-day-slot dedupe), the line rides
prana's router (body if Suti is present, else Telegram), and the sent
nudge is recorded back into akhada's store. No LLM anywhere in this
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
    "https://intergalactic.tail807360.ts.net:8799/talk")


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
    result = route_utterance(text, source="akhada-checkin", topic="fitness")
    if not result.ok:
        release_checkin(store, out["slot"], out["day"])
        print(f"nudge NOT delivered (claim released): "
              f"body={result.body_error} telegram={result.telegram_error}")
        return 1

    confirm_checkin(store, out["slot"], out["day"], result.delivered_to,
                    out["text"])
    print(f"nudge sent via {result.delivered_to} [{out['slot']}]: "
          f"{out['text']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
