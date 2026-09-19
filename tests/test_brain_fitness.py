"""Akhada's briefing rides the chat session by default and refreshes
per turn when today or the rules moved (plan-exercise-corpus §C2)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from prana.brain import fitness
from prana.brain.config import BrainConfig
from prana.brain.session import SessionPool


class _Backend:
    instances: list["_Backend"] = []

    def __init__(self, **kw):
        self.kw = kw
        self.prompts: list = []
        _Backend.instances.append(self)

    async def start(self):
        pass

    async def close(self):
        pass

    async def run_turn(self, prompt):
        self.prompts.append(prompt)
        yield "ok"

    async def cancel(self):
        pass

    @property
    def native_session_id(self):
        return None


@pytest.fixture
def provider():
    calls = {"n": 0}
    state = {"current": True}

    def fake_briefing(surface="chat"):
        calls["n"] += 1
        return SimpleNamespace(text=f"B{calls['n']} {surface}", as_of="2026-09-15", rules_rev=calls["n"])

    def fake_is_current(store, as_of, rev):
        return state["current"]

    fitness.set_provider(fake_briefing, fake_is_current)
    yield state
    fitness.set_provider(None, None)


async def _pool(tmp_path, monkeypatch):
    wake = tmp_path / "wake.md"
    wake.write_text("wake", encoding="utf-8")
    pool = SessionPool(BrainConfig(sessions_root=tmp_path / "s", wake_context=wake), _Backend)
    monkeypatch.setattr(pool, "_tier_mcp", lambda tier: {})
    return pool


async def _turn(session, text):
    session.reserve()
    try:
        return [c async for c in session.run_turn(text, deadline_s=5.0)]
    finally:
        session.release()


async def test_the_briefing_opens_the_session_and_refreshes_only_when_stale(tmp_path, monkeypatch, provider):
    _Backend.instances = []
    pool = await _pool(tmp_path, monkeypatch)
    session = await pool.get_or_create("s1", "app")
    b = _Backend.instances[0]
    assert b.kw["system_append"].endswith("AKHADA (today, computed from his log — act on it, don't re-fetch):\nB1 chat")
    await _turn(session, "hello")
    assert b.prompts == ["hello"]  # still current: nothing prepended
    provider["current"] = False  # midnight, or a rule changed elsewhere
    await _turn(session, "again")
    assert b.prompts[1] == "TODAY (updated since this session opened):\nB2 chat\n\nagain"
    provider["current"] = True  # the new stamp holds
    await _turn(session, [{"type": "text", "text": "third"}])
    assert b.prompts[2] == [{"type": "text", "text": "third"}]
    provider["current"] = False
    await _turn(session, [{"type": "text", "text": "fourth"}])
    assert b.prompts[3][0] == {"type": "text", "text": "TODAY (updated since this session opened):\nB3 chat"}


async def test_no_akhada_means_no_block_and_no_failure(tmp_path, monkeypatch):
    fitness.set_provider(None, None)
    monkeypatch.setattr(fitness, "_load", lambda: None)
    _Backend.instances = []
    pool = await _pool(tmp_path, monkeypatch)
    session = await pool.get_or_create("s2", "app")
    assert "AKHADA" not in _Backend.instances[0].kw["system_append"]
    await _turn(session, "hello")
    assert _Backend.instances[0].prompts == ["hello"]


def test_a_broken_briefing_yields_nothing_and_keeps_the_stamp():
    def boom(surface="chat"):
        raise RuntimeError("db locked")

    fitness.set_provider(boom, lambda *a: False)
    try:
        assert fitness.briefing_block("voice") == ("", None)
        assert fitness.refresh_block(("2026-09-15", 3), "voice") == (None, ("2026-09-15", 3))
    finally:
        fitness.set_provider(None, None)
