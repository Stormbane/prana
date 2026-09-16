"""Tool servers checked, respawned once, and said plainly (Suti
2026-09-16: "Why didn't we know it was down already and spin it back
up? we need it to be more resilient")."""

from __future__ import annotations

import sys

from prana.brain import mcp_probe
from prana.brain.config import BrainConfig
from prana.brain.session import SessionPool

PY = sys.executable

_ANSWERS = (
    "import sys, json; msg = json.loads(sys.stdin.readline()); "
    "print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': {'ok': True}}), flush=True)"
)
_DIES = "import sys; sys.stderr.write('ModuleNotFoundError: No module named akhada\\n'); sys.exit(1)"


def test_probe_tells_an_answering_server_from_a_dead_one_and_a_missing_command(tmp_path):
    answering = {"command": PY, "args": ["-c", _ANSWERS]}
    dying = {"command": PY, "args": ["-c", _DIES]}
    missing = {"command": str(tmp_path / "nope.exe"), "args": []}
    assert mcp_probe.probe_one("ok", answering, timeout=30) is None
    why = mcp_probe.probe_one("dead", dying, timeout=30)
    assert why is not None and why.startswith("exited rc=1") and "akhada" in why
    gone = mcp_probe.probe_one("gone", missing, timeout=30)
    assert gone is not None and "command not found" in gone
    assert list(mcp_probe.probe({"ok": answering, "dead": dying}, timeout=30)) == ["dead"]
    assert mcp_probe.probe({}, timeout=1) == {}


def test_report_posts_one_error_entry_per_server_per_day(monkeypatch):
    posted: list[dict] = []
    monkeypatch.setattr("prana.brain.push.notify", lambda **kw: posted.append(kw) or {})
    mcp_probe.report({"akhada": "exited rc=1: ModuleNotFoundError"}, "brain startup")
    assert len(posted) == 1
    assert posted[0]["level"] == "error"
    assert "akhada" in posted[0]["body"] and "brain startup" in posted[0]["body"]
    assert posted[0]["event_id"].startswith("tool-server-failed:akhada:")


def test_check_reports_only_failures(monkeypatch):
    seen: list = []
    monkeypatch.setattr(mcp_probe, "probe", lambda servers, timeout: {"akhada": "dead"})
    monkeypatch.setattr(mcp_probe, "report", lambda failed, where: seen.append((failed, where)))
    assert mcp_probe.check({"akhada": {}}, "brain startup") == {"akhada": "dead"}
    assert seen == [({"akhada": "dead"}, "brain startup")]
    monkeypatch.setattr(mcp_probe, "probe", lambda servers, timeout: {})
    assert mcp_probe.check({"akhada": {}}, "brain startup") == {}
    assert len(seen) == 1


class _Backend:
    """Reports akhada dead on the first spawn only."""

    instances: list["_Backend"] = []

    def __init__(self, **kw):
        self.kw = kw
        self.started = False
        self.closed = False
        _Backend.instances.append(self)

    async def start(self):
        self.started = True

    async def close(self):
        self.closed = True

    async def failed_mcp_servers(self):
        return {"akhada": "Connection closed"} if self is _Backend.instances[0] else {}

    async def run_turn(self, prompt):
        yield "hi"

    async def cancel(self):
        pass

    @property
    def native_session_id(self):
        return None


class _DeadBackend(_Backend):
    """akhada dead every time."""

    async def failed_mcp_servers(self):
        return {"akhada": "Connection closed"}


def _pool(tmp_path, factory, monkeypatch) -> SessionPool:
    wake = tmp_path / "wake.md"
    wake.write_text("w", encoding="utf-8")
    pool = SessionPool(BrainConfig(sessions_root=tmp_path / "s", wake_context=wake), factory)
    monkeypatch.setattr(pool, "_tier_mcp", lambda tier: {})
    return pool


async def test_a_tool_server_dead_at_spawn_gets_one_fresh_spawn(tmp_path, monkeypatch):
    _Backend.instances = []
    pool = _pool(tmp_path, _Backend, monkeypatch)
    session = await pool.get_or_create("s1", "app")
    assert len(_Backend.instances) == 2
    first, second = _Backend.instances
    assert first.started and first.closed
    assert second.started and not second.closed
    assert session.backend is second


async def test_a_server_dead_twice_is_reported_and_the_session_still_opens(tmp_path, monkeypatch):
    _Backend.instances = []
    seen: list = []
    monkeypatch.setattr("prana.brain.mcp_probe.report", lambda failed, where: seen.append((failed, where)))
    pool = _pool(tmp_path, _DeadBackend, monkeypatch)
    session = await pool.get_or_create("s2", "app")
    assert len(_Backend.instances) == 2  # one respawn, not a loop
    assert session.backend is _Backend.instances[1]
    assert seen == [({"akhada": "Connection closed"}, "session app:s2")]
