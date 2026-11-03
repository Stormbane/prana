"""A resume whose Claude transcript Claude Code has deleted starts fresh
with a handover instead of failing every turn ("No conversation found",
doctor 2026-11-03)."""

from __future__ import annotations

import asyncio

from prana.brain.config import BrainConfig
from prana.brain.session import SessionPool


class _Fake:
    made: list["_Fake"] = []

    def __init__(self, name, **kw):
        self.name = name
        self.kw = kw
        self._native = kw.get("resume")
        _Fake.made.append(self)

    async def start(self):
        pass

    async def run_turn(self, prompt):
        self._native = "claude-native-1"
        yield "ok"

    async def cancel(self):
        pass

    async def close(self):
        pass

    @property
    def native_session_id(self):
        return self._native


def _factory(**kw):
    return _Fake("claude", **kw)


def _config(tmp_path, transcripts_root):
    wake = tmp_path / "wake.md"
    wake.write_text("ctx", encoding="utf-8")
    return BrainConfig(sessions_root=tmp_path / "sessions", wake_context=wake,
                       claude_transcripts_root=transcripts_root)


async def _first_turn(config):
    """A session that has talked once and persisted its native id."""
    _Fake.made.clear()
    pool = SessionPool(config, _factory)
    s = await pool.get_or_create("chat", "app", "sonnet")
    s.append_transcript("user", "remember the fish")
    s.append_transcript("assistant", "Noted: fish after the fast.")
    async for _ in s.backend.run_turn("x"):
        pass
    s._persist_native_id()


def test_missing_transcript_starts_fresh_with_a_handover(tmp_path):
    projects = tmp_path / "claude-projects"
    projects.mkdir()
    config = _config(tmp_path, projects)

    async def go():
        await _first_turn(config)
        sdir = config.sessions_root / "app" / "chat"
        assert (sdir / "native-session-id").read_text(encoding="utf-8") == "claude-native-1"
        # restart: Claude Code has no transcript for claude-native-1
        pool = SessionPool(config, _factory)
        await pool.get_or_create("chat", "app", None)
        made = _Fake.made[-1]
        assert made.kw["resume"] is None
        assert made.kw["handover"] and "remember the fish" in made.kw["handover"]
        assert "transcript expired" in made.kw["handover"]
        assert (sdir / "native-session-id").read_text(encoding="utf-8") == ""
        assert (sdir / "pending-handover.md").exists()
    asyncio.run(go())


def test_existing_transcript_is_resumed(tmp_path):
    projects = tmp_path / "claude-projects"
    (projects / "C--some-project").mkdir(parents=True)
    (projects / "C--some-project" / "claude-native-1.jsonl").write_text("{}", encoding="utf-8")
    config = _config(tmp_path, projects)

    async def go():
        await _first_turn(config)
        pool = SessionPool(config, _factory)
        await pool.get_or_create("chat", "app", None)
        made = _Fake.made[-1]
        assert made.kw["resume"] == "claude-native-1"
        assert made.kw["handover"] is None
    asyncio.run(go())


def test_unconfigured_root_keeps_the_old_behaviour(tmp_path):
    config = _config(tmp_path, None)

    async def go():
        await _first_turn(config)
        pool = SessionPool(config, _factory)
        await pool.get_or_create("chat", "app", None)
        assert _Fake.made[-1].kw["resume"] == "claude-native-1"
    asyncio.run(go())
