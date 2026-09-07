"""Scheduler observer (retire-hermes plan, §Scheduler observer).

Two pieces: AlertManager.observe_transition (edge-triggered pager over
the durable outbox) and Supervisor._evaluate_scheduler_health (the pure
stale/missing/fresh/grace decision).
"""

from __future__ import annotations

import json
import time

from prana.host import supervisor as sup_mod
from prana.host.alerts import AlertManager
from prana.host.supervisor import (
    DEADMAN_PROGRESS_GRACE_S,
    DEADMAN_PROGRESS_STALE_S,
    Supervisor,
)


# --- AlertManager.observe_transition ---------------------------------

def _mgr(tmp_path):
    sent = []
    mgr = AlertManager(db_path=tmp_path / "alerts.db",
                       send=lambda t: (True, None, ""))
    return mgr, sent


def _outbox_texts(mgr):
    return [r["text"] for r in mgr._conn.execute(
        "SELECT text FROM host_alert_outbox ORDER BY id").fetchall()]


def test_observe_transition_pages_once_on_unhealthy_edge(tmp_path):
    mgr, _ = _mgr(tmp_path)
    mgr.observe_transition("scheduler-deadman", False, "not firing", "ok again")
    mgr.observe_transition("scheduler-deadman", False, "not firing", "ok again")
    texts = _outbox_texts(mgr)
    assert len(texts) == 1  # latched — no second page
    assert "not firing" in texts[0]


def test_observe_transition_recovery_edge(tmp_path):
    mgr, _ = _mgr(tmp_path)
    mgr.observe_transition("scheduler-deadman", False, "not firing", "ok again")
    mgr.observe_transition("scheduler-deadman", True, "not firing", "ok again")
    texts = _outbox_texts(mgr)
    assert len(texts) == 2
    assert "ok again" in texts[1]


def test_observe_transition_healthy_from_start_is_silent(tmp_path):
    mgr, _ = _mgr(tmp_path)
    mgr.observe_transition("scheduler-deadman", True, "not firing", "ok again")
    assert _outbox_texts(mgr) == []


def test_observe_transition_recovery_only_after_alert(tmp_path):
    # healthy while not latched must not enqueue a spurious recovery
    mgr, _ = _mgr(tmp_path)
    for _ in range(3):
        mgr.observe_transition("scheduler-deadman", True, "p", "r")
    assert _outbox_texts(mgr) == []


# --- Supervisor._evaluate_scheduler_health ---------------------------

def _sup():
    class _StubAlerts:
        def __getattr__(self, n): return lambda *a, **k: None
    return Supervisor(components=[], alerts=_StubAlerts())


def test_grace_when_beacon_absent_and_freshly_started(tmp_path, monkeypatch):
    monkeypatch.setattr(sup_mod, "deadman_progress_file",
                        lambda: tmp_path / "nope.progress")
    sup = _sup()
    now = 1000.0
    started = now - 10  # just started
    assert sup._evaluate_scheduler_health(now, started) is None


def test_missing_beacon_after_grace_is_unhealthy(tmp_path, monkeypatch):
    monkeypatch.setattr(sup_mod, "deadman_progress_file",
                        lambda: tmp_path / "nope.progress")
    sup = _sup()
    now = 1_000_000.0
    started = now - (DEADMAN_PROGRESS_GRACE_S + 60)
    healthy, problem = sup._evaluate_scheduler_health(now, started)
    assert healthy is False
    assert "no deadman progress beacon" in problem


def test_fresh_beacon_is_healthy(tmp_path, monkeypatch):
    p = tmp_path / "deadman.progress"
    monkeypatch.setattr(sup_mod, "deadman_progress_file", lambda: p)
    now = 1_000_000.0
    p.write_text(json.dumps({"ts": now - 60, "pid": 1}), encoding="utf-8")
    sup = _sup()
    healthy, _ = sup._evaluate_scheduler_health(now, now - 10_000)
    assert healthy is True


def test_stale_beacon_is_unhealthy(tmp_path, monkeypatch):
    p = tmp_path / "deadman.progress"
    monkeypatch.setattr(sup_mod, "deadman_progress_file", lambda: p)
    now = 1_000_000.0
    p.write_text(json.dumps({"ts": now - (DEADMAN_PROGRESS_STALE_S + 120), "pid": 1}),
                 encoding="utf-8")
    sup = _sup()
    healthy, problem = sup._evaluate_scheduler_health(now, now - 10_000)
    assert healthy is False
    assert "last ran" in problem


def test_corrupt_beacon_is_unhealthy_after_grace(tmp_path, monkeypatch):
    p = tmp_path / "deadman.progress"
    monkeypatch.setattr(sup_mod, "deadman_progress_file", lambda: p)
    p.write_text("{ not json", encoding="utf-8")
    now = 1_000_000.0
    sup = _sup()
    healthy, _ = sup._evaluate_scheduler_health(now, now - (DEADMAN_PROGRESS_GRACE_S + 60))
    assert healthy is False


def test_bom_beacon_is_read_healthy(tmp_path, monkeypatch):
    # The deadman writes the beacon via PowerShell 5.1 Set-Content -Encoding
    # utf8, which prepends a BOM. The host MUST read it (utf-8-sig), or
    # every healthy beacon is rejected and a live scheduler reads as dead.
    p = tmp_path / "deadman.progress"
    monkeypatch.setattr(sup_mod, "deadman_progress_file", lambda: p)
    now = 1_000_000.0
    body = json.dumps({"ts": now - 60, "pid": 5}).encode("utf-8")
    p.write_bytes(b"\xef\xbb\xbf" + body)  # UTF-8 BOM + payload
    sup = _sup()
    healthy, _ = sup._evaluate_scheduler_health(now, now - 10_000)
    assert healthy is True


def test_external_check_not_double_paged_by_sweep(tmp_path):
    # observe_transition marks the row external so the deadline-driven
    # sweep() skips it — otherwise sweep re-pages "sick" after 30 min
    # on top of the edge page (Codex review).
    clock = {"t": 1_000_000.0}
    mgr = AlertManager(db_path=tmp_path / "a.db",
                       send=lambda t: (True, None, ""),
                       now=lambda: clock["t"])
    mgr.observe_transition("scheduler-deadman", False, "not firing", "ok")
    before = len(mgr._conn.execute(
        "SELECT id FROM host_alert_outbox").fetchall())
    assert before == 1
    clock["t"] += 1801  # > EPISODE_ALERT_DEADLINE_S
    mgr.sweep()
    after = len(mgr._conn.execute(
        "SELECT id FROM host_alert_outbox").fetchall())
    assert after == 1  # sweep did NOT add a second page
