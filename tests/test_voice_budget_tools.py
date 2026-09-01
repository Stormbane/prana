"""Voice loop: budget guards and the closed tool surface."""

from __future__ import annotations

import threading

import pytest

from prana.voice.budget import BudgetExceeded, BudgetUnavailable, VoiceBudget


def _budget(tmp_path, **kw):
    return VoiceBudget(tmp_path / "ledger.json", **kw)


def test_reserve_settle_roundtrip(tmp_path):
    b = _budget(tmp_path, daily_cap_min=30, session_cap_s=10 * 60)
    rid = b.reserve()
    b.settle(rid, 9 * 60)
    assert b.spent_today_min() == pytest.approx(9.0)


def test_reservations_bound_concurrent_admission(tmp_path):
    """Cap 25 min, sessions reserve 10 min each: only two may enter."""
    b = _budget(tmp_path, daily_cap_min=25, session_cap_s=10 * 60)
    admitted, rejected = [], []

    def worker():
        try:
            admitted.append(b.reserve())
        except BudgetExceeded:
            rejected.append(1)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(admitted) == 2 and len(rejected) == 4


def test_settle_releases_unused_reservation(tmp_path):
    b = _budget(tmp_path, daily_cap_min=15, session_cap_s=10 * 60)
    rid = b.reserve()
    with pytest.raises(BudgetExceeded):
        b.reserve()  # 10 reserved + 10 requested > 15
    b.settle(rid, 60)  # actual use: 1 minute
    b.reserve()  # now fits


def test_corrupt_ledger_fails_closed(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text("{corrupt", encoding="utf-8")
    b = VoiceBudget(path, daily_cap_min=10)
    with pytest.raises(BudgetUnavailable):
        b.reserve()
    with pytest.raises(BudgetUnavailable):
        b.spent_today_min()


def test_midnight_split_attributes_both_days(tmp_path):
    import json
    from datetime import datetime, timedelta

    b = _budget(tmp_path, daily_cap_min=1000)
    rid = b.reserve()
    # rewrite the reservation to have started 30 min before midnight
    data = json.loads((tmp_path / "ledger.json").read_text())
    yesterday_start = datetime.combine(
        datetime.now().date(), datetime.min.time()
    ) - timedelta(minutes=30)
    data["reservations"][rid]["start_iso"] = yesterday_start.isoformat()
    (tmp_path / "ledger.json").write_text(json.dumps(data))
    b.settle(rid, 60 * 60)  # one hour spanning midnight
    ledger = json.loads((tmp_path / "ledger.json").read_text())["days"]
    days = sorted(ledger)
    assert len(days) == 2
    assert ledger[days[0]] == pytest.approx(30.0)  # yesterday
    assert ledger[days[1]] == pytest.approx(30.0)  # today


VOICE_TOOL_NAMES = {
    "list_coding_sessions",
    "list_open_terminals",
    "read_session_output",
    "request_session_action",
    "recall_memory",
    # B1 (resilience-and-reach, ratified 2026-08-28): writes ONLY to
    # the quarantined inbox/voice — never to a recallable branch. The
    # quarantine sentinel lives in test_voice_remember.py.
    "remember_this",
    "escalate_to_narada",
    # round 10: "Narada, stop" hangs up via the normal sleep path
    "end_conversation",
    # B5 (ratified): music is shareable-tier — playing a station
    # discloses nothing; the audio-owner state machine pauses it for
    # every session and wake-word admission is off while it plays.
    "play_music",
    "stop_music",
    "what_is_playing",
    "set_volume",       # speaker loudness via device protocol
    "set_music_volume",
    # B4 (ratified): reads of the public web, SSRF-contracted in
    # web.py (validated resolution, pinned connections, per-hop
    # redirect checks). Adversarial matrix in test_voice_web.py.
    "web_search",
    "read_page",
}
FORBIDDEN_NAMES = {
    "spawn_session", "relay_instruction", "cancel_session",
    "decide_proposal", "resume_foreign_session",
    # memory recall must be the voice-safe projection, never raw smriti
    "smriti_read",
    # memory WRITES must be the quarantined inbox path, never raw smriti
    "smriti_write",
}


def test_voice_tool_surface_is_closed(tmp_path):
    """The realtime model gets exactly these tools — a new mutation tool
    appearing here is a sovereignty regression, not a feature."""
    from prana.sessions.escalate import ProposalQueue
    from prana.sessions.service import ServiceClient
    from prana.voice.tools import build_voice_tools

    tools = build_voice_tools(
        client=ServiceClient(port=1, token="unused"),
        proposals=ProposalQueue(tmp_path / "s.db"),
    )
    names = set()
    for t in tools:
        info = getattr(t, "info", None) or getattr(t, "_info", None)
        name = getattr(info, "name", None) or getattr(t, "__name__", None)
        names.add(name)
    assert names == VOICE_TOOL_NAMES
    assert FORBIDDEN_NAMES.isdisjoint(names)
