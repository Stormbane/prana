"""Voice loop: budget guards and the closed tool surface."""

from __future__ import annotations

import pytest

from prana.voice.budget import BudgetExceeded, VoiceBudget


def _budget(tmp_path, **kw):
    return VoiceBudget(tmp_path / "ledger.json", **kw)


def test_budget_accumulates_and_caps(tmp_path):
    b = _budget(tmp_path, daily_cap_min=10)
    b.check_can_start()  # fresh day: fine
    b.record_session(9 * 60)
    assert b.spent_today_min() == pytest.approx(9.0)
    b.check_can_start()  # 9 < 10: still fine
    b.record_session(2 * 60)
    with pytest.raises(BudgetExceeded):
        b.check_can_start()


def test_budget_survives_corrupt_ledger(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text("{corrupt", encoding="utf-8")
    b = VoiceBudget(path, daily_cap_min=10)
    assert b.spent_today_min() == 0.0
    b.record_session(60)
    assert b.spent_today_min() == pytest.approx(1.0)


VOICE_TOOL_NAMES = {
    "list_coding_sessions",
    "list_open_terminals",
    "read_session_output",
    "request_session_action",
}
FORBIDDEN_NAMES = {
    "spawn_session", "relay_instruction", "cancel_session",
    "decide_proposal", "resume_foreign_session",
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
