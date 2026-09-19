"""Akhada's briefing rides the chat session by default and refreshes
per turn when today or the rules moved (plan-exercise-corpus §C2) —
and every way akhada can fail leaves the session standing (Codex
diff review, 2026-09-20)."""

from __future__ import annotations

import asyncio
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
        self.fail_next = False
        _Backend.instances.append(self)

    async def start(self):
        pass

    async def close(self):
        pass

    async def run_turn(self, prompt):
        self.prompts.append(prompt)
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("transport died before sending")
        yield "ok"

    async def cancel(self):
        pass

    @property
    def native_session_id(self):
        return None


@pytest.fixture
def provider():
    calls = {"n": 0}
    state = {"current": True, "broken": False}

    def fake_briefing(surface="chat"):
        if state["broken"]:
            raise RuntimeError("db locked")
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


async def test_a_refresh_the_backend_failed_to_deliver_is_tried_again(tmp_path, monkeypatch, provider):
    _Backend.instances = []
    pool = await _pool(tmp_path, monkeypatch)
    session = await pool.get_or_create("s1", "app")
    b = _Backend.instances[0]
    provider["current"] = False
    b.fail_next = True
    with pytest.raises(RuntimeError):
        await _turn(session, "lost")
    assert b.prompts[0].startswith("TODAY (updated since this session opened):\nB2 chat")
    assert session.brief_stamp == ("2026-09-15", 1)  # not advanced: nothing was delivered
    await _turn(session, "retry")
    assert b.prompts[1].startswith("TODAY (updated since this session opened):\nB3 chat")
    assert session.brief_stamp == ("2026-09-15", 3)


async def test_an_opening_failure_is_made_good_on_a_later_turn(tmp_path, monkeypatch, provider):
    _Backend.instances = []
    provider["broken"] = True
    pool = await _pool(tmp_path, monkeypatch)
    session = await pool.get_or_create("s1", "app")
    b = _Backend.instances[0]
    assert "AKHADA" not in b.kw["system_append"] and session.brief_stamp is None
    await _turn(session, "one")
    assert b.prompts[0] == "one"  # still broken: nothing, no failure
    provider["broken"] = False
    await _turn(session, "two")
    assert b.prompts[1] == "AKHADA (today, computed from his log — act on it, don't re-fetch):\nB1 chat\n\ntwo"
    assert session.brief_stamp == ("2026-09-15", 1)
    await _turn(session, "three")
    assert b.prompts[2] == "three"


async def test_no_akhada_means_no_block_and_no_failure(tmp_path, monkeypatch):
    fitness.set_provider(None, None)

    def no_akhada():
        raise ImportError("no module named akhada")

    monkeypatch.setattr(fitness, "_load", no_akhada)
    _Backend.instances = []
    pool = await _pool(tmp_path, monkeypatch)
    session = await pool.get_or_create("s2", "app")
    assert "AKHADA" not in _Backend.instances[0].kw["system_append"]
    await _turn(session, "hello")
    assert _Backend.instances[0].prompts == ["hello"]


def test_every_akhada_failure_yields_nothing(monkeypatch):
    # import-time failure (a bad AKHADA_TZ raises from akhada.clock)
    def boom_import():
        raise LookupError("No time zone found")

    monkeypatch.setattr(fitness, "_load", boom_import)
    assert fitness.briefing_block("voice") == ("", None)
    assert fitness.refresh_block(("2026-09-15", 3), "voice") == (None, ("2026-09-15", 3))
    monkeypatch.undo()
    # a malformed result
    fitness.set_provider(lambda surface="chat": SimpleNamespace(text="x"), lambda *a: False)
    try:
        assert fitness.briefing_block("chat") == ("", None)
        assert fitness.refresh_block(None, "chat") == (None, None)
    finally:
        fitness.set_provider(None, None)
    # a check that raises reads as current: a stale line beats a dead turn
    fitness.set_provider(lambda surface="chat": SimpleNamespace(text="t", as_of="d", rules_rev=1),
                         lambda *a: (_ for _ in ()).throw(RuntimeError("locked")))
    try:
        assert fitness.refresh_block(("d", 1), "chat") == (None, ("d", 1))
    finally:
        fitness.set_provider(None, None)


def test_refresh_without_a_stamp_is_the_opening_block():
    fitness.set_provider(lambda surface="chat": SimpleNamespace(text="T", as_of="2026-09-15", rules_rev=4),
                         lambda *a: True)
    try:
        block, stamp = fitness.refresh_block(None, "voice")
        assert block == "FITNESS (Akhada):\nT" and stamp == ("2026-09-15", 4)
    finally:
        fitness.set_provider(None, None)


def test_the_hooked_agent_runs_the_refresh_before_the_reply_and_survives_it():
    pytest.importorskip("livekit.agents")
    from prana.voice.hooked import HookedAgent

    seen = []

    async def hook():
        seen.append("refreshed")

    async def slow():
        await asyncio.sleep(5)

    async def broken():
        raise RuntimeError("no db")

    async def run():
        from livekit.agents import llm

        for h, expect in ((hook, ["refreshed"]), (slow, ["refreshed"]), (broken, ["refreshed"])):
            agent = HookedAgent(instructions="x", before_reply=h, hook_timeout_s=0.05)
            await agent.on_user_turn_completed(llm.ChatContext(), llm.ChatMessage(role="user", content=["hi"]))
            assert seen == expect

    asyncio.run(run())


def test_the_transcript_trigger_schedules_the_refresh_on_final_only():
    pytest.importorskip("livekit.agents")
    from prana.voice.hooked import attach_refresh

    class FakeSession:
        def __init__(self):
            self.handlers = {}

        def on(self, name):
            def deco(fn):
                self.handlers[name] = fn
                return fn
            return deco

    runs = []

    async def refresh():
        runs.append(1)

    async def run():
        s = FakeSession()
        attach_refresh(s, refresh)
        h = s.handlers["user_input_transcribed"]
        h(SimpleNamespace(is_final=False))
        h(SimpleNamespace(is_final=True))
        h(SimpleNamespace(is_final=True))
        await asyncio.sleep(0)
        assert runs == [1, 1]

    asyncio.run(run())

