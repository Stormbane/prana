"""C1 — timers/reminders: durable, bounded, idempotent, tier-ruled."""

from __future__ import annotations

from pathlib import Path

import pytest

from prana.voice import timers


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def sent_ok(text):
    sent_ok.calls.append(text)
    return {"delivered": True}


def test_create_bounds(tmp_path: Path):
    db = tmp_path / "s.db"
    with pytest.raises(timers.TimerError):
        timers.create("x", 1.0, kind="timer", tier="personal",
                      session_id="s", db_path=db)  # too soon
    with pytest.raises(timers.TimerError):
        timers.create("x", 15 * 24 * 3600.0, kind="reminder",
                      tier="personal", session_id="s", db_path=db)
    with pytest.raises(timers.TimerError):
        timers.create("", 60.0, kind="timer", tier="personal",
                      session_id="s", db_path=db)
    for i in range(timers.MAX_PENDING):
        timers.create(f"t{i}", 600.0, kind="timer", tier="personal",
                      session_id="s", db_path=db)
    with pytest.raises(timers.TimerError):
        timers.create("overflow", 600.0, kind="timer", tier="personal",
                      session_id="s", db_path=db)


def test_fire_is_idempotent_across_restarts(tmp_path: Path):
    db = tmp_path / "s.db"
    clock = Clock()
    sent_ok.calls = []
    timers.create("tea", 60.0, kind="timer", tier="personal",
                  session_id="s", db_path=db, now=clock)
    clock.t += 61
    fired = timers.sweep_due(db_path=db, now=clock, send_personal=sent_ok)
    assert len(fired) == 1
    assert fired[0]["delivered"] == "telegram"
    # A second sweep ("restarted worker") must not re-fire.
    fired2 = timers.sweep_due(db_path=db, now=clock, send_personal=sent_ok)
    assert fired2 == []
    assert len(sent_ok.calls) == 1


def test_shareable_never_reaches_telegram(tmp_path: Path):
    """Cross-review #6: a shareable-tier entry must not become delayed
    Telegram delivery, even when it cannot be announced locally."""
    db = tmp_path / "s.db"
    clock = Clock()
    sent_ok.calls = []
    timers.create("guest timer", 60.0, kind="timer", tier="shareable",
                  session_id="s", db_path=db, now=clock)
    clock.t += 61
    fired = timers.sweep_due(db_path=db, now=clock,
                             announce_local=None, send_personal=sent_ok)
    assert len(fired) == 1
    assert sent_ok.calls == []              # telegram untouched
    assert fired[0]["delivered"] == "none"  # recorded, not hidden


def test_local_announce_preferred_over_telegram(tmp_path: Path):
    db = tmp_path / "s.db"
    clock = Clock()
    sent_ok.calls = []
    timers.create("stretch", 60.0, kind="reminder", tier="personal",
                  session_id="s", db_path=db, now=clock)
    clock.t += 61
    fired = timers.sweep_due(db_path=db, now=clock,
                             announce_local=lambda e: True,
                             send_personal=sent_ok)
    assert fired[0]["delivered"] == "body"
    assert sent_ok.calls == []


def test_personal_cancel_requires_personal_tier(tmp_path: Path):
    db = tmp_path / "s.db"
    r = timers.create("private", 600.0, kind="reminder", tier="personal",
                      session_id="s", db_path=db)
    with pytest.raises(timers.TimerError):
        timers.cancel(r["id"], tier="shareable", db_path=db)
    assert timers.cancel(r["id"], tier="personal", db_path=db) is True
    # Shareable entries are cancellable by anyone present.
    r2 = timers.create("kitchen", 600.0, kind="timer", tier="shareable",
                       session_id="s", db_path=db)
    assert timers.cancel(r2["id"], tier="shareable", db_path=db) is True


def test_telegram_failure_recorded_not_silent(tmp_path: Path):
    db = tmp_path / "s.db"
    clock = Clock()
    timers.create("bins", 60.0, kind="reminder", tier="personal",
                  session_id="s", db_path=db, now=clock)
    clock.t += 61
    fired = timers.sweep_due(
        db_path=db, now=clock,
        send_personal=lambda text: {"delivered": False, "detail": "cap"})
    assert fired[0]["delivered"].startswith("telegram-failed")
