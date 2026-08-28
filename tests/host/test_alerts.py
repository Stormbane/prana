"""A3 — durable alert state machine (resilience-and-reach).

The contract under test: at most one page per failure episode, pages
survive host restarts (no duplicates, no losses), time-based transitions
fire from the sweep with no intervening events, and nothing secret
reaches a phone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from prana.host.alerts import (
    EPISODE_ALERT_DEADLINE_S,
    HFT_STORM_N,
    RECOVERY_HEALTHY_S,
    AlertManager,
)


class Clock:
    def __init__(self, t0: float = 1_000_000.0):
        self.t = t0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class Sender:
    """Programmable fake Telegram sender."""

    def __init__(self):
        self.sent: list[str] = []
        self.mode = "ok"           # ok | fail | 429
        self.retry_after = 120.0

    def __call__(self, text: str):
        if self.mode == "ok":
            self.sent.append(text)
            return True, None, ""
        if self.mode == "429":
            return False, self.retry_after, "HTTP 429"
        return False, None, "ConnectionError"


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


def make(db: Path, clock: Clock, sender: Sender) -> AlertManager:
    return AlertManager(db_path=db, send=sender, now=clock)


def pump(mgr: AlertManager) -> None:
    mgr.sweep()
    mgr.drain_outbox()


def test_cooldown_pages_once_then_recovery_once(db):
    clock, sender = Clock(), Sender()
    mgr = make(db, clock, sender)

    mgr.record_cooldown("livekit")
    pump(mgr)
    assert len(sender.sent) == 1
    assert "livekit" in sender.sent[0] and "cooldown" in sender.sent[0]

    # More sweeps, more failures inside the same episode: no re-page.
    mgr.record_exit("livekit", 1)
    clock.advance(120)
    pump(mgr)
    assert len(sender.sent) == 1

    # Healthy long enough -> exactly one recovery, episode closes.
    mgr.record_health("livekit", ok=True)
    clock.advance(RECOVERY_HEALTHY_S + 1)
    pump(mgr)
    assert len(sender.sent) == 2
    assert "recovered" in sender.sent[1]
    clock.advance(120)
    pump(mgr)
    assert len(sender.sent) == 2


def test_hft_storm_pages_at_threshold(db):
    clock, sender = Clock(), Sender()
    mgr = make(db, clock, sender)

    for _ in range(HFT_STORM_N - 1):
        mgr.record_health_fail_termination("voice")
        clock.advance(60)
    pump(mgr)
    assert sender.sent == []

    mgr.record_health_fail_termination("voice")
    pump(mgr)
    assert len(sender.sent) == 1
    assert "health-fail terminations" in sender.sent[0]


def test_deadline_pages_across_manager_restart_without_events(db):
    """The R2-1 case: the 30-min deadline must fire from the sweep alone,
    even when the host itself was down when the deadline passed."""
    clock, sender = Clock(), Sender()
    mgr = make(db, clock, sender)
    mgr.record_health("voice", ok=False)
    pump(mgr)
    assert sender.sent == []  # not yet — below every threshold

    # Host "restarts"; the deadline passes while nothing is running.
    del mgr
    clock.advance(EPISODE_ALERT_DEADLINE_S + 60)
    sender2 = Sender()
    mgr2 = make(db, clock, sender2)
    pump(mgr2)
    assert len(sender2.sent) == 1
    assert "unhealthy for" in sender2.sent[0]

    # Another restart: the alerted flag is durable — no duplicate.
    del mgr2
    sender3 = Sender()
    mgr3 = make(db, clock, sender3)
    pump(mgr3)
    assert sender3.sent == []


def test_transient_blip_closes_silently(db):
    clock, sender = Clock(), Sender()
    mgr = make(db, clock, sender)
    mgr.record_exit("chat-bridge", 1)
    mgr.record_spawned("chat-bridge", has_health_probe=False)
    clock.advance(RECOVERY_HEALTHY_S + 1)
    pump(mgr)
    assert sender.sent == []  # no page for a single restart
    row = mgr._conn.execute(
        "SELECT episode_open FROM host_alert_status WHERE component = ?",
        ("chat-bridge",)).fetchone()
    assert row["episode_open"] == 0


def test_outbox_retries_with_backoff_and_survives_restart(db):
    clock, sender = Clock(), Sender()
    sender.mode = "fail"
    mgr = make(db, clock, sender)
    mgr.record_cooldown("livekit")
    pump(mgr)
    assert sender.sent == []  # enqueued, delivery failed

    # Immediate re-drain must NOT hammer: next attempt is in the future.
    mgr.drain_outbox()
    row = mgr._conn.execute(
        "SELECT attempts FROM host_alert_outbox").fetchone()
    assert row["attempts"] == 1

    # Manager restart + connectivity back -> the queued alert delivers.
    del mgr
    clock.advance(3600)
    sender2 = Sender()
    mgr2 = make(db, clock, sender2)
    mgr2.drain_outbox()
    assert len(sender2.sent) == 1


def test_429_honors_retry_after(db):
    clock, sender = Clock(), Sender()
    sender.mode = "429"
    sender.retry_after = 300.0
    mgr = make(db, clock, sender)
    mgr.record_cooldown("voice")
    pump(mgr)
    row = mgr._conn.execute(
        "SELECT next_attempt_at FROM host_alert_outbox").fetchone()
    assert row["next_attempt_at"] == pytest.approx(clock.t + 300.0)

    sender.mode = "ok"
    clock.advance(299)
    mgr.drain_outbox()
    assert sender.sent == []          # still inside Retry-After
    clock.advance(2)
    mgr.drain_outbox()
    assert len(sender.sent) == 1


def test_diagnostics_redacted_and_bounded(db):
    clock, sender = Clock(), Sender()
    mgr = make(db, clock, sender)
    secret_line = ("POST https://api.telegram.org/bot8654654226:"
                   "AAGHbWZItfxIU-grGjHe5IxdLyE-XIW_vYU/getUpdates "
                   "sk-abcdefghijklmnop1234 ") + "x" * 500
    mgr.note_diagnostic("chat-bridge", secret_line)
    mgr.record_cooldown("chat-bridge")
    pump(mgr)
    assert len(sender.sent) == 1
    msg = sender.sent[0]
    assert "AAGHbWZItfxIU" not in msg
    assert "sk-abcdefghijklmnop1234" not in msg
    assert "[REDACTED]" in msg
    assert len(msg) < 600


def test_probed_spawn_clears_stale_healthy_since(db):
    """Review P2: after a host outage, a stale healthy_since must not
    age past the recovery threshold and close an episode before the new
    process passes a single probe."""
    clock, sender = Clock(), Sender()
    mgr = make(db, clock, sender)
    mgr.record_cooldown("livekit")
    mgr.record_health("livekit", ok=True)   # brief recovery begins
    pump(mgr)
    assert len(sender.sent) == 1            # the episode alert

    # Host dies; downtime exceeds the recovery window; host restarts
    # and respawns the component — which has NOT passed a probe yet.
    del mgr
    clock.advance(RECOVERY_HEALTHY_S + 120)
    sender2 = Sender()
    mgr2 = make(db, clock, sender2)
    mgr2.record_spawned("livekit", has_health_probe=True)
    pump(mgr2)
    assert sender2.sent == []               # no premature recovery

    # Only a real probe success starts the recovery clock.
    mgr2.record_health("livekit", ok=True)
    clock.advance(RECOVERY_HEALTHY_S + 1)
    pump(mgr2)
    assert len(sender2.sent) == 1 and "recovered" in sender2.sent[0]
