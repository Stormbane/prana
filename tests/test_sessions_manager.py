"""SessionManager: caps, event flow, cancel with a real process tree."""

from __future__ import annotations

import subprocess
import sys
import time

import psutil
import pytest

import prana.sessions.manager as manager_mod
from prana.sessions.adapters import SessionEvent
from prana.sessions.jobobject import JobObject
from prana.sessions.manager import CapExceeded, ManagerConfig, SessionManager
from prana.sessions.registry import SessionState


class FakeProc:
    """Stands in for SpawnedProcess in manager tests."""

    _next_pid = 91000

    def __init__(self, on_event):
        FakeProc._next_pid += 1
        self.pid = FakeProc._next_pid
        self.on_event = on_event
        self.killed = False
        self.started = False

    def start(self):
        self.started = True

    def kill(self):
        self.killed = True


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    spawned = []

    def fake_spawn(prompt, cwd, on_event, *, resume_session_id=None):
        fp = FakeProc(on_event)
        spawned.append(fp)
        return fp

    monkeypatch.setitem(manager_mod.SPAWNERS, "claude", fake_spawn)
    monkeypatch.setitem(manager_mod.SPAWNERS, "codex", fake_spawn)
    m = SessionManager(ManagerConfig(
        db_path=tmp_path / "s.db", global_cap=3, per_provider_cap=2,
    ))
    m._spawned_fakes = spawned
    return m


def test_spawn_and_event_flow(mgr):
    sess = mgr.spawn("claude", r"C:\p", "do the thing")
    assert sess.state is SessionState.RUNNING
    fake = mgr._spawned_fakes[0]
    fake.on_event(SessionEvent(kind="init", provider_session_id="psid-1"))
    assert mgr.get(sess.id).provider_session_id == "psid-1"
    fake.on_event(SessionEvent(kind="exit", raw={"exit_code": 0}))
    assert mgr.get(sess.id).state is SessionState.DONE


def test_nonzero_exit_marks_dead(mgr):
    sess = mgr.spawn("claude", r"C:\p", "x")
    mgr._spawned_fakes[0].on_event(
        SessionEvent(kind="exit", raw={"exit_code": 3})
    )
    after = mgr.get(sess.id)
    assert after.state is SessionState.DEAD
    assert after.exit_code == 3


def test_caps_enforced(mgr):
    mgr.spawn("claude", r"C:\p", "a")
    mgr.spawn("claude", r"C:\p", "b")
    with pytest.raises(CapExceeded):
        mgr.spawn("claude", r"C:\p", "c")   # per-provider cap = 2
    mgr.spawn("codex", r"C:\p", "d")
    with pytest.raises(CapExceeded):
        mgr.spawn("codex", r"C:\p", "e")    # global cap = 3


def test_duplicate_spawn_returns_existing(mgr):
    a = mgr.spawn("claude", r"C:\p", "task", idempotency_key="k1")
    b = mgr.spawn("claude", r"C:\p", "task", idempotency_key="k1")
    assert a.id == b.id
    assert len(mgr._spawned_fakes) == 1  # no second process


def test_cancel_kills(mgr):
    sess = mgr.spawn("claude", r"C:\p", "x")
    after = mgr.cancel(sess.id)
    assert after.state is SessionState.KILLED
    assert mgr._spawned_fakes[0].killed is True


@pytest.mark.skipif(sys.platform != "win32", reason="Job Objects are Windows")
def test_jobobject_kills_real_process_tree():
    """The ctypes layer, exercised for real: spawn python -> kill via job."""
    job = JobObject()
    assert job.active
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        assert job.assign(proc.pid) is True
        assert psutil.pid_exists(proc.pid)
        job.kill()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.05)
        assert proc.poll() is not None, "job kill did not reap the process"
    finally:
        if proc.poll() is None:
            proc.kill()
