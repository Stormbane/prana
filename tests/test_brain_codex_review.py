"""Codex backend — the review round (2026-09-09): tier boundaries, the
writable workspace, process-tree cancellation, legacy sessions and a
handover that survives a restart."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time

import pytest

from prana.brain.codex_backend import (
    CodexBackend, build_command, host_mcp_servers, kill_tree,
)
from prana.brain.config import CODEX_MODELS, BrainConfig
from prana.brain.session import SessionPool


def test_host_servers_are_off_unless_the_tier_wires_them(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text(
        '[mcp_servers.smriti]\ncommand = "python"\n'
        '[mcp_servers.akhada]\ncommand = "python"\n'
        '[mcp_servers.privileged]\ncommand = "python"\n'
        '[mcp_servers.supabase]\nurl = "https://x"\n', encoding="utf-8")
    hosts = host_mcp_servers(tmp_path)
    assert hosts == {"smriti", "akhada", "privileged", "supabase"}
    assert host_mcp_servers(tmp_path / "nowhere") == set()
    monkeypatch.setenv("NARADA_CODEX_BIN", "codex.exe")
    tier = {"smriti": {"command": "py", "args": ["-m", "smriti.mcp_server"]},
            "narada-notes": {"command": "py", "args": ["-m", "prana.notes.mcp"]}}
    cmd = build_command(model="m", reasoning="low", disabled_servers=("playwright",), images=[],
                        resume=None, mcp_servers=tier, host_servers=hosts)
    off = [c for c in cmd if c.endswith(".enabled=false")]
    assert set(off) == {"mcp_servers.akhada.enabled=false", "mcp_servers.privileged.enabled=false",
                        "mcp_servers.supabase.enabled=false", "mcp_servers.playwright.enabled=false"}
    assert "mcp_servers.smriti.enabled=true" in cmd
    assert "mcp_servers.narada-notes.enabled=true" in cmd  # wired even though the host lacks it
    assert "mcp_servers.smriti.enabled=false" not in cmd


def test_the_workspace_is_a_scratch_subdirectory(tmp_path):
    b = CodexBackend(model="m", system_append="ctx", mcp_servers={}, max_tool_iterations=20,
                     cwd=str(tmp_path / "s"), codex_home=tmp_path / "home", images_dir=tmp_path / "img")
    asyncio.run(b.start())
    assert (tmp_path / "s" / "codex" / "AGENTS.md").exists()
    assert not (tmp_path / "s" / "AGENTS.md").exists()
    # session state one level up stays outside the workspace
    assert b._cwd == tmp_path / "s" / "codex"


@pytest.mark.skipif(sys.platform != "win32", reason="the shim case is a Windows one")
def test_kill_tree_takes_the_descendants_too():
    async def go():
        # cmd.exe → python sleeping: killing cmd alone would orphan python
        proc = await asyncio.create_subprocess_exec(
            "cmd", "/c", sys.executable, "-c", "import time; time.sleep(60)",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.sleep(1.5)
        listing = subprocess.run(
            ["wmic", "process", "where", f"ParentProcessId={proc.pid}", "get", "ProcessId"],
            capture_output=True, text=True, check=False).stdout
        children = [int(x) for x in listing.split() if x.isdigit()]
        assert children, listing
        await kill_tree(proc)
        await asyncio.wait_for(proc.wait(), 10)
        time.sleep(1.0)
        for pid in children:
            alive = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True,
                                   text=True, check=False).stdout
            assert str(pid) not in alive, f"child {pid} survived: {alive}"
    asyncio.run(go())


# ── the pool ─────────────────────────────────────────────────────────

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
        yield "ok"

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


def _config(tmp_path):
    wake = tmp_path / "wake.md"
    wake.write_text("ctx", encoding="utf-8")
    return BrainConfig(sessions_root=tmp_path / "sessions", wake_context=wake)


def _pool(config):
    _Fake.made.clear()
    return SessionPool(config, _factory("claude"), {"codex": _factory("codex")})


def test_a_legacy_claude_session_is_handed_over_not_resumed_by_codex(tmp_path):
    config = _config(tmp_path)
    sdir = config.sessions_root / "app" / "old"
    sdir.mkdir(parents=True)
    (sdir / "native-session-id").write_text("claude-legacy-id", encoding="utf-8")
    (sdir / "model").write_text("sonnet", encoding="utf-8")
    (sdir / "transcript.jsonl").write_text(
        json.dumps({"role": "user", "content": "the old days"}) + "\n", encoding="utf-8")
    # no "backend" file: from before backends were recorded

    async def go():
        pool = _pool(config)
        # a cold switch straight to Codex (the pin is rewritten first, as the API does)
        s = await pool.switch_model("old", "app", CODEX_MODELS[0])
        made = _Fake.made[-1]
        assert made.name == "codex" and made.kw["resume"] is None
        assert made.kw["handover"] and "the old days" in made.kw["handover"]
        assert s.model == CODEX_MODELS[0]
        # and a legacy session touched under its own model resumes as Claude, no handover
        sdir2 = config.sessions_root / "app" / "old2"
        sdir2.mkdir(parents=True)
        (sdir2 / "native-session-id").write_text("claude-legacy-2", encoding="utf-8")
        (sdir2 / "model").write_text("sonnet", encoding="utf-8")
        await pool.get_or_create("old2", "app", "sonnet")
        same = _Fake.made[-1]
        assert same.name == "claude" and same.kw["resume"] == "claude-legacy-2"
        assert same.kw["handover"] is None
    asyncio.run(go())


def test_the_handover_survives_a_restart_and_clears_once_a_turn_completes(tmp_path):
    config = _config(tmp_path)

    async def go():
        pool = _pool(config)
        s = await pool.get_or_create("chat", "app", "sonnet")
        s.append_transcript("user", "remember the milk")
        async for _ in s.backend.run_turn("x"):
            pass
        s._persist_native_id()
        await pool.switch_model("chat", "app", CODEX_MODELS[0])
        sdir = config.sessions_root / "app" / "chat"
        pending = sdir / "pending-handover.md"
        assert pending.exists() and "remember the milk" in pending.read_text(encoding="utf-8")
        # the brain restarts before the first Codex turn: a fresh pool
        pool2 = _pool(config)
        s2 = await pool2.get_or_create("chat", "app", None)
        made = _Fake.made[-1]
        assert made.name == "codex" and made.kw["resume"] is None
        assert made.kw["handover"] and "remember the milk" in made.kw["handover"]
        # a completed turn retires it
        s2.reserve()
        async for _ in s2.run_turn("hello", deadline_s=5):
            pass
        s2.release()
        assert not pending.exists()
        # and the next recreation carries nothing extra
        pool3 = _pool(config)
        await pool3.get_or_create("chat", "app", None)
        assert _Fake.made[-1].kw["handover"] is None
    asyncio.run(go())


def test_a_failed_first_turn_keeps_the_pending_handover(tmp_path):
    config = _config(tmp_path)

    class _Boom(_Fake):
        async def run_turn(self, prompt):
            raise RuntimeError("codex could not be started")
            yield  # pragma: no cover

    async def go():
        pool = _pool(config)
        s = await pool.get_or_create("chat", "app", "sonnet")
        s.append_transcript("user", "keep this")
        async for _ in s.backend.run_turn("x"):
            pass
        s._persist_native_id()
        pool._backends["codex"] = lambda **kw: _Boom("codex", **kw)
        s2 = await pool.switch_model("chat", "app", CODEX_MODELS[0])
        pending = config.sessions_root / "app" / "chat" / "pending-handover.md"
        s2.reserve()
        with pytest.raises(RuntimeError):
            async for _ in s2.run_turn("hello", deadline_s=5):
                pass
        s2.release()
        assert pending.exists()  # still owed to the new backend
    asyncio.run(go())
