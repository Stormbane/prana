"""PID-reuse safety and token-initialization concurrency.

Codex recheck findings: (1) a reused pid must never be killed on the
strength of pid_exists alone; (2) concurrent first-run token
initialization must converge on ONE set of tokens.
"""

from __future__ import annotations

import threading

import prana.sessions.manager as manager_mod
from prana.sessions.manager import ManagerConfig, SessionManager, _session_alive
from prana.sessions.registry import Session, SessionState
from prana.sessions.tokens import TOKEN_KEYS, load_or_create_tokens


def _sess(pid, pid_created_at, state=SessionState.RUNNING):
    return Session(
        id="s1", provider="claude", cwd="C:/p", state=state,
        pid=pid, pid_created_at=pid_created_at,
    )


def test_alive_requires_identity_match(monkeypatch):
    monkeypatch.setattr(manager_mod, "_proc_create_time", lambda pid: 1000.0)
    assert _session_alive(_sess(pid=77, pid_created_at=1000.0)) is True
    # same pid, different create_time -> reused pid, NOT our session
    assert _session_alive(_sess(pid=77, pid_created_at=999998.0)) is False


def test_alive_legacy_row_falls_back_to_existence(monkeypatch):
    monkeypatch.setattr(manager_mod, "_proc_create_time", lambda pid: 1000.0)
    assert _session_alive(_sess(pid=77, pid_created_at=None)) is True
    monkeypatch.setattr(manager_mod, "_proc_create_time", lambda pid: None)
    assert _session_alive(_sess(pid=77, pid_created_at=None)) is False


def test_cancel_refuses_reused_pid(tmp_path, monkeypatch):
    """A live registry row whose pid now belongs to someone else: cancel
    must NOT kill, and must record DEAD (we killed nothing), not KILLED."""
    mgr = SessionManager(ManagerConfig(db_path=tmp_path / "s.db"))
    sess = mgr.registry.create("claude", "C:/p")
    mgr.registry.transition(
        sess.id, SessionState.RUNNING, pid=4242, pid_created_at=1000.0
    )
    # pid 4242 exists but was created at a different time (reused)
    monkeypatch.setattr(manager_mod, "_proc_create_time", lambda pid: 2000.0)

    killed = {"called": False}

    class NeverKill:
        def __init__(self, pid):
            killed["called"] = True
            raise AssertionError("must not touch a reused pid")

    monkeypatch.setattr(manager_mod.psutil, "Process", NeverKill)
    after = mgr.cancel(sess.id)
    assert killed["called"] is False
    assert after.state is SessionState.DEAD
    assert "reused" in (after.last_error or "") or "gone" in (after.last_error or "")


def test_token_init_concurrent_converges(tmp_path):
    path = tmp_path / "tokens.json"
    results = []
    errors = []

    def worker():
        try:
            results.append(load_or_create_tokens(path))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(results) == 8
    first = results[0]
    assert all(r == first for r in results), "divergent tokens issued"
    assert all(first.get(k) for k in TOKEN_KEYS)
    # file on disk matches what everyone got
    assert load_or_create_tokens(path) == first
