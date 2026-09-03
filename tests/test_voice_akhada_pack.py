"""Akhada's fitness tool pack rides the personal tier only."""

from __future__ import annotations

import pytest


def _names(tools) -> set[str]:
    names = set()
    for t in tools:
        info = getattr(t, "info", None) or getattr(t, "_info", None)
        names.add(getattr(info, "name", None) or getattr(t, "__name__", None))
    return names


@pytest.fixture
def _build(tmp_path, monkeypatch):
    monkeypatch.setenv("AKHADA_DB", str(tmp_path / "akhada.db"))
    from prana.sessions.escalate import ProposalQueue
    from prana.sessions.service import ServiceClient
    from prana.voice.tools import build_voice_tools

    def build(tier: str):
        return build_voice_tools(
            client=ServiceClient(port=1, token="unused"),
            proposals=ProposalQueue(tmp_path / "s.db"), tier=tier)
    return build


def test_akhada_tools_only_on_the_personal_tier(_build):
    pytest.importorskip("akhada")
    shareable = _names(_build("shareable"))
    personal = _names(_build("personal"))
    assert "log_meal" not in shareable and "get_today_summary" not in shareable
    assert {"log_meal", "log_vital", "log_workout", "amend_entry", "void_entry",
            "get_today_summary", "get_recent", "get_goals", "list_desires"} <= personal


def test_missing_akhada_costs_nothing(_build, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith("akhada"):
            raise ImportError("no akhada here")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    personal = _names(_build("personal"))
    assert "log_meal" not in personal and "message_suti" in personal
