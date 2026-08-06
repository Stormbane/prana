"""Registry lifecycle: the plan's named failure modes.

Covers: duplicate spawn (idempotency), manager restart (reconcile),
pane closed by hand, hang detection (timeout sweep), partial spawn
failure, and illegal-transition enforcement.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from prana.sessions.registry import (
    IllegalTransition,
    SessionRegistry,
    SessionState,
)


@pytest.fixture
def reg(tmp_path):
    return SessionRegistry(tmp_path / "sessions.db")


def _spawned(reg, provider="claude", key=None):
    sess = reg.create(provider, r"C:\proj", idempotency_key=key)
    return reg.transition(sess.id, SessionState.RUNNING, pid=4242)


def test_lifecycle_happy_path(reg):
    sess = reg.create("claude", r"C:\proj", title="t")
    assert sess.state is SessionState.SPAWNING
    sess = reg.transition(sess.id, SessionState.RUNNING, pid=123)
    assert sess.pid == 123
    sess = reg.transition(sess.id, SessionState.IDLE)
    sess = reg.transition(sess.id, SessionState.DONE, exit_code=0)
    assert sess.ended_at is not None


def test_illegal_transition_rejected(reg):
    sess = reg.create("claude", r"C:\proj")
    with pytest.raises(IllegalTransition):
        reg.transition(sess.id, SessionState.IDLE)  # spawning -> idle
    done = _spawned(reg)
    done = reg.transition(done.id, SessionState.DONE)
    with pytest.raises(IllegalTransition):
        reg.transition(done.id, SessionState.RUNNING)  # terminal is terminal


def test_duplicate_spawn_idempotency(reg):
    a = _spawned(reg, key="voice-cmd-77")
    b = reg.create("claude", r"C:\other", idempotency_key="voice-cmd-77")
    assert b.id == a.id  # retried command returns the same session
    assert len(reg.list()) == 1


def test_manager_restart_reconcile_marks_dead(reg):
    live = _spawned(reg)
    # restart: process is gone
    marked = reg.reconcile(session_alive=lambda s: False)
    assert [s.id for s in marked] == [live.id]
    assert reg.get(live.id).state is SessionState.DEAD
    # a session whose process survives is untouched
    survivor = _spawned(reg, key="k2")
    assert reg.reconcile(session_alive=lambda s: True) == []
    assert reg.get(survivor.id).state is SessionState.RUNNING


def test_pane_closed_clears_mapping_but_keeps_session(reg):
    sess = _spawned(reg)
    reg.transition(sess.id, SessionState.IDLE, pane_id="7")
    reg.reconcile(session_alive=lambda s: True, live_pane_ids=["1", "2"])
    after = reg.get(sess.id)
    assert after.pane_id is None          # stale mapping cleared
    assert after.state is SessionState.IDLE  # session still live


def test_partial_spawn_failure(reg):
    sess = reg.create("kimi", r"C:\proj")
    failed = reg.transition(sess.id, SessionState.FAILED, error="CLI not found")
    assert failed.state is SessionState.FAILED
    assert failed.last_error == "CLI not found"
    assert failed.ended_at is not None


def test_hang_detection_sweep(reg, monkeypatch):
    sess = _spawned(reg)
    # backdate activity far past both thresholds
    conn = reg._conn()
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    conn.execute(
        "UPDATE sessions SET last_activity_at = ?, updated_at = ? WHERE id = ?",
        (old, old, sess.id),
    )
    conn.close()
    changed = reg.sweep_timeouts(idle_after_s=120, hung_after_s=1800)
    assert reg.get(sess.id).state is SessionState.IDLE
    # second sweep: still stale -> hung
    conn = reg._conn()
    conn.execute(
        "UPDATE sessions SET last_activity_at = ?, updated_at = ? WHERE id = ?",
        (old, old, sess.id),
    )
    conn.close()
    reg.sweep_timeouts(idle_after_s=120, hung_after_s=1800)
    assert reg.get(sess.id).state is SessionState.HUNG
    # hung is killable
    assert reg.transition(sess.id, SessionState.KILLED).state is SessionState.KILLED


def test_touch_wakes_idle(reg):
    sess = _spawned(reg)
    reg.transition(sess.id, SessionState.IDLE)
    reg.touch(sess.id)
    assert reg.get(sess.id).state is SessionState.RUNNING
