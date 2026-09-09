"""The Codex backend (Suti 2026-09-09): OpenAI models on the ChatGPT
subscription behind the same backend protocol. The wire is tested
from lines recorded off codex-cli 0.153.4; the process itself is not
spawned here."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from prana.brain.cards import ActivityEvent, CardEvent
from prana.brain.codex_backend import (
    CodexBackend, TurnReader, build_command, harvest_images,
)
from prana.brain.config import CODEX_MODELS, MODELS, BrainConfig, backend_for
from prana.brain.session import SessionPool, handover_text

RECORDED = [
    {"type": "thread.started", "thread_id": "01a08655-3a88-75d2-9712-2a7c33ae1971"},
    {"type": "item.completed", "item": {"id": "item_0", "type": "error",
                                        "message": "clamping SessionEnd hook timeout to 3s in hooks.json"}},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message",
                                        "text": "I’ll check today’s protein."}},
    {"type": "item.started", "item": {"id": "item_3", "type": "mcp_tool_call", "server": "akhada",
                                      "tool": "get_today_summary", "arguments": {"date": "2026-09-09"},
                                      "result": None, "error": None, "status": "in_progress"}},
    {"type": "item.completed", "item": {"id": "item_3", "type": "mcp_tool_call", "server": "akhada",
                                        "tool": "get_today_summary", "arguments": {"date": "2026-09-09"},
                                        "result": {"content": [{"type": "text", "text": json.dumps(
                                            {"ok": True, "day": "2026-09-09", "totals": {},
                                             "meals": [], "remaining": {"protein_g": 180}})}]},
                                        "error": None, "status": "completed"}},
    {"type": "item.started", "item": {"id": "item_4", "type": "command_execution",
                                      "command": "powershell -Command Get-Content x", "aggregated_output": "",
                                      "exit_code": None, "status": "in_progress"}},
    {"type": "item.completed", "item": {"id": "item_4", "type": "command_execution",
                                        "command": "powershell -Command Get-Content x", "aggregated_output": "…",
                                        "exit_code": 0, "status": "completed"}},
    {"type": "item.completed", "item": {"id": "item_5", "type": "agent_message", "text": "180 g to go."}},
    {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
]


def test_recorded_turn_becomes_text_chips_and_a_card():
    reader = TurnReader()
    out = []
    for ev in RECORDED:
        out.extend(reader.feed(ev))
    assert reader.thread_id == "01a08655-3a88-75d2-9712-2a7c33ae1971"
    assert reader.completed and reader.failed is None
    assert reader.errors == []  # the hook-timeout clamp is noise
    texts = [o for o in out if isinstance(o, str)]
    assert texts == ["I’ll check today’s protein.", "\n\n180 g to go."]
    acts = [o.activity for o in out if isinstance(o, ActivityEvent)]
    assert acts[0] == {"text": "Checking today", "kind": "fitness", "id": "item_3", "state": "running"}
    cards = [o.spec for o in out if isinstance(o, CardEvent)]
    assert len(cards) == 1 and cards[0]["caption"] == "2026-09-09 so far"
    assert cards[0]["activity_id"] == "item_3"
    assert acts[1] == {"id": "item_3", "state": "done", "card_id": cards[0]["card_id"]}
    assert acts[2] == {"text": "Running a command", "kind": "files", "id": "item_4", "state": "running"}
    assert acts[3] == {"id": "item_4", "state": "done"}


def test_a_refused_mcp_call_and_a_failed_turn():
    reader = TurnReader()
    reader.feed({"type": "item.started", "item": {"id": "t", "type": "mcp_tool_call", "server": "akhada",
                                                  "tool": "log_set", "arguments": {}, "status": "in_progress"}})
    out = reader.feed({"type": "item.completed", "item": {
        "id": "t", "type": "mcp_tool_call", "server": "akhada", "tool": "log_set", "arguments": {},
        "result": None, "error": {"message": "MCP tool call requires approval"}, "status": "failed"}})
    assert out == [ActivityEvent({"id": "t", "state": "failed"})]
    reader.feed({"type": "turn.failed", "error": {"message": "rate limited"}})
    assert reader.failed == "rate limited"
    # malformed lines never raise
    for bad in ({}, {"type": "item.completed"}, {"type": "item.completed", "item": "x"},
                {"type": "item.completed", "item": {"type": "agent_message", "text": 7}}):
        assert TurnReader().feed(bad) == []


def test_command_puts_flags_before_resume_and_the_prompt_on_stdin(monkeypatch):
    monkeypatch.setenv("NARADA_CODEX_BIN", "C:/codex/codex.exe")
    cmd = build_command(model="gpt-6-astra", reasoning="medium",
                        disabled_servers=("supabase", "playwright"),
                        images=["C:/s/inbox/a.png"], resume="thread-1")
    assert cmd[:2] == ["C:/codex/codex.exe", "exec"]
    assert "--json" in cmd and "--approve-for-me" in cmd and "--skip-git-repo-check" in cmd
    assert cmd[cmd.index("-m") + 1] == "gpt-6-astra"
    assert 'model_reasoning_effort="medium"' in cmd
    assert not any(c.endswith(".enabled=false") for c in cmd)
    assert cmd[cmd.index("-i") + 1] == "C:/s/inbox/a.png"
    assert cmd[-2:] == ["resume", "thread-1"]  # the subcommand last, parent flags before it
    assert not any("\n" in c for c in cmd)
    fresh = build_command(model="m", reasoning="low", disabled_servers=(), images=[], resume=None)
    assert "resume" not in fresh
    monkeypatch.setenv("NARADA_CODEX_BIN", "C:/npm/codex.cmd")
    assert build_command(model="m", reasoning="low", disabled_servers=(), images=[], resume=None)[:3] == [
        "cmd", "/c", "C:/npm/codex.cmd"]


def test_generated_images_become_cards_in_the_store(tmp_path):
    home = tmp_path / "codex"
    folder = home / "generated_images" / "thread-9"
    folder.mkdir(parents=True)
    old = folder / "old.png"
    old.write_bytes(b"\x89PNG old")
    import os
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    since = time.time()
    new = folder / "exec-abc.png"
    new.write_bytes(b"\x89PNG new")
    (folder / "notes.txt").write_text("x")
    store = tmp_path / "images"
    cards = harvest_images(home, "thread-9", since, store)
    assert len(cards) == 1
    spec = cards[0].spec
    assert spec["kind"] == "image" and spec["caption"] == "Narada drew this"
    name = spec["data"]["url"].rsplit("/", 1)[1]
    assert (store / name).read_bytes() == b"\x89PNG new"
    assert "activity_id" not in spec  # its own message, shown outright
    assert harvest_images(home, None, since, store) == []
    assert harvest_images(home, "no-such-thread", since, store) == []


def test_agents_md_carries_the_instructions_and_images_go_to_files(tmp_path):
    b = CodexBackend(model="gpt-6-astra", system_append="I am Narada.\n## Charter\nno code",
                     mcp_servers={}, max_tool_iterations=20, cwd=str(tmp_path / "s"),
                     codex_home=tmp_path, images_dir=tmp_path / "img")
    asyncio.run(b.start())
    text = (tmp_path / "s" / "codex" / "AGENTS.md").read_text(encoding="utf-8")
    assert text.startswith("I am Narada.") and "no code" in text and "imagegen" in text
    import base64
    prompt = [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                           "data": base64.b64encode(b"\x89PNG").decode()}},
              {"type": "text", "text": "what is this"}]
    t, images = b._materialize(prompt)
    assert t == "what is this" and len(images) == 1
    assert Path(images[0]).read_bytes() == b"\x89PNG"
    assert b._materialize("plain") == ("plain", [])


def test_registry_names_backends():
    assert backend_for("sonnet") == "claude" and backend_for("opus") == "claude"
    assert CODEX_MODELS and all(backend_for(m) == "codex" for m in CODEX_MODELS)
    assert set(CODEX_MODELS) <= set(MODELS)
    assert backend_for("nonsense") == "claude"


# ── crossing backends: the pool hands the conversation over ───────────

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
        self._native = f"{self.name}-native"
        yield f"hello from {self.name}"

    async def cancel(self):
        pass

    async def close(self):
        pass

    @property
    def native_session_id(self):
        return self._native


def _factory(name):
    def make(**kw):
        return _Fake(name, **kw)
    return make


@pytest.fixture
def pool(tmp_path, monkeypatch):
    wake = tmp_path / "wake.md"
    wake.write_text("ctx", encoding="utf-8")
    config = BrainConfig(sessions_root=tmp_path / "sessions", wake_context=wake)
    _Fake.made.clear()
    return SessionPool(config, _factory("claude"), {"codex": _factory("codex")})


def test_switching_backends_drops_the_resume_and_hands_over_the_transcript(pool):
    async def go():
        codex_model = CODEX_MODELS[0]
        s = await pool.get_or_create("chat", "app", "sonnet")
        assert _Fake.made[-1].name == "claude" and _Fake.made[-1].kw["handover"] is None
        s.append_transcript("user", "remember the milk")
        s.append_transcript("assistant", "Noted: milk.")
        s.reserve()
        async for _ in s.run_turn("x", deadline_s=5):
            pass
        s.release()
        s2 = await pool.switch_model("chat", "app", codex_model)
        made = _Fake.made[-1]
        assert made.name == "codex"
        assert made.kw["resume"] is None  # a Claude id means nothing to Codex
        assert "remember the milk" in made.kw["handover"] and "Noted: milk." in made.kw["handover"]
        assert made.kw["handover"].startswith("[Context")
        assert s2.model == codex_model
        assert (pool._config.sessions_root / "app" / "chat" / "backend").read_text() == "codex"
        # and back again: the Codex id is not handed to Claude either
        s2.reserve()
        async for _ in s2.run_turn("y", deadline_s=5):
            pass
        s2.release()
        s3 = await pool.switch_model("chat", "app", "haiku")
        back = _Fake.made[-1]
        assert back.name == "claude" and back.kw["resume"] is None and back.kw["handover"]
        # the Codex id is gone from disk too, not merely skipped once
        assert (pool._config.sessions_root / "app" / "chat" / "native-session-id").read_text() == ""
        # same backend: resume as before, no handover
        s3.reserve()
        async for _ in s3.run_turn("z", deadline_s=5):
            pass
        s3.release()
        await pool.switch_model("chat", "app", "sonnet")
        same = _Fake.made[-1]
        assert same.kw["resume"] == "claude-native" and same.kw["handover"] is None
    asyncio.run(go())


def test_handover_text_is_bounded_and_none_when_empty(tmp_path):
    assert handover_text(tmp_path) is None
    path = tmp_path / "transcript.jsonl"
    lines = [json.dumps({"role": "user", "content": f"turn {i} " + "x" * 500}) for i in range(100)]
    lines.append("not json")
    lines.append(json.dumps({"role": "system", "content": "ignored"}))
    path.write_text("\n".join(lines), encoding="utf-8")
    text = handover_text(tmp_path)
    assert text is not None and len(text) <= 12_000 + 200
    assert "turn 99" in text and "turn 0 " not in text and "ignored" not in text


def test_models_endpoint_names_backends_and_images_are_served(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from prana.brain.api import create_app

    wake = tmp_path / "wake.md"
    wake.write_text("ctx", encoding="utf-8")
    images = tmp_path / "images"
    images.mkdir()
    (images / "abc123.png").write_bytes(b"\x89PNG")
    config = BrainConfig(sessions_root=tmp_path / "sessions", wake_context=wake, images_dir=images)
    monkeypatch.setattr("prana.brain.api.load_brain_tokens", lambda: {"app": "tok-app"})
    with TestClient(create_app(config, _factory("claude"))) as c:
        h = {"Authorization": "Bearer tok-app"}
        rows = c.get("/v1/models", headers=h).json()["data"]
        by_id = {r["id"]: r["backend"] for r in rows}
        assert by_id["sonnet"] == "claude" and by_id[CODEX_MODELS[0]] == "codex"
        assert c.get("/api/images/abc123.png", headers=h).content == b"\x89PNG"
        assert c.get("/api/images/abc123.png").status_code == 401
        assert c.get("/api/images/nope.png", headers=h).status_code == 404
        assert c.get("/api/images/..%2Fwake.md", headers=h).status_code in (400, 404)
        assert c.get("/api/images/Abc.PNG", headers=h).status_code == 400


def test_the_brain_s_mcp_wiring_rides_as_overrides(monkeypatch):
    from prana.brain.codex_backend import mcp_overrides

    servers = {
        "akhada": {"command": r"C:\ProgramData\anaconda3\python.exe",
                   "args": ["-m", "akhada.adapters.mcp_server"]},
        "narada-sessions": {"command": "py", "args": ["-m", "x"], "env": {"PRANA_SESSIONS_TOKEN": "t\"q"}},
        "broken": "not a spec",
    }
    ov = mcp_overrides(servers)
    assert "mcp_servers.akhada.enabled=true" in ov
    assert f"mcp_servers.akhada.command={json.dumps(servers['akhada']['command'])}" in ov
    assert 'mcp_servers.akhada.args=["-m", "akhada.adapters.mcp_server"]' in ov
    assert f"mcp_servers.narada-sessions.env.PRANA_SESSIONS_TOKEN={json.dumps('t' + chr(34) + 'q')}" in ov
    assert not any("broken" in o for o in ov)
    monkeypatch.setenv("NARADA_CODEX_BIN", "codex.exe")
    cmd = build_command(model="m", reasoning="low", disabled_servers=("supabase",), images=[],
                        resume="t1", mcp_servers=servers)
    assert cmd.index("mcp_servers.akhada.enabled=true") < cmd.index("resume")


def test_codex_housekeeping_calls_get_no_chip():
    from prana.brain.cards import activity_for

    assert activity_for("mcp__x__list_mcp_resources", {}) is None
    assert activity_for("mcp__x__list_mcp_resource_templates", {}) is None
