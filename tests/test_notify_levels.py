"""Notification levels (plan §T7): routing, episodes, idempotent
producer events, replay/delivered-channels, the producer door."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from prana.brain import push
from prana.brain.api import create_app
from prana.brain.config import BrainConfig


@pytest.fixture(autouse=True)
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    yield tmp_path


def _notify(**kw):
    kw.setdefault("webpush_fn", lambda **_: None)
    kw.setdefault("telegram_fn", lambda _t: True)
    return push.notify(**kw)


def test_routing_table():
    assert push.route("log") == ()
    assert push.route("warn") == ("badge",)
    assert "push" in push.route("error") and "telegram" in push.route("error")
    assert "escalate" in push.route("emergency")
    assert push.route("nonsense") == push.route(push.DEFAULT_LEVEL)


def test_feed_records_every_level_but_pushes_error_plus():
    log = _notify(body="recovered", level="log", source="host")
    warn = _notify(body="cooldown", level="warn", source="host")
    err = _notify(body="down", level="error", source="host")
    assert log["channels"] == [] and log["push"] is None
    assert warn["channels"] == ["badge"] and warn["push"] is None
    assert "push" in err["channels"] and err["telegram"] is True
    assert [r["level"] for r in push.read_notifications()] == ["log", "warn", "error"]


def test_episode_collapses_repeats_and_escalation_repages():
    a = _notify(body="storm 1", level="warn", source="host", category="x/storm")
    b = _notify(body="storm 2", level="warn", source="host", category="x/storm")
    rows = push.read_notifications()
    assert len(rows) == 1 and rows[0]["count"] == 2 and rows[0]["body"] == "storm 2"
    assert b["channels"] == ["badge"]  # a repeat only badges
    c = _notify(body="still down", level="error", source="host", category="x/storm")
    rows = push.read_notifications()
    assert len(rows) == 1 and rows[0]["level"] == "error" and rows[0]["count"] == 3
    assert c["escalated"] is True and "push" in c["channels"]  # escalation re-pages
    # Recovery closes the episode; the next occurrence is a NEW row.
    _notify(body="back", level="log", source="host", category="x/storm")
    _notify(body="storm again", level="warn", source="host", category="x/storm")
    rows = push.read_notifications()
    assert [r["level"] for r in rows] == ["error", "log", "warn"]
    assert rows[0]["open"] is False and rows[-1]["count"] == 1
    assert a["new"] is True


def test_uncategorized_never_groups():
    _notify(body="one", level="error", source="host")
    _notify(body="two", level="error", source="host")
    assert len(push.read_notifications()) == 2


def test_event_id_is_idempotent_and_replay_is_feed_only():
    first = _notify(body="emergency!", level="emergency", source="deadman",
                    event_id="evt-1", delivered_channels=["telegram"])
    assert first["telegram"] is None  # producer already paged Telegram
    assert "push" in first["channels"]
    again = _notify(body="emergency!", level="emergency", source="deadman",
                    event_id="evt-1")
    assert again["new"] is False and again["channels"] == []
    assert len(push.read_notifications()) == 1
    replay = _notify(body="old emergency", level="emergency", source="deadman",
                     event_id="evt-0", replay=True)
    assert replay["channels"] == ["badge"] and replay["push"] is None


def test_legacy_jsonl_migrates_once():
    root = Path.home() / ".narada" / "brain"
    root.mkdir(parents=True)
    (root / "notifications.jsonl").write_text(
        json.dumps({"at": "2026-09-07T10:00:00+1000", "title": "Narada", "body": "old"}) + "\n",
        encoding="utf-8")
    rows = push.read_notifications()
    assert rows[0]["body"] == "old" and rows[0]["level"] == "error"
    _notify(body="new", level="log", source="x")
    assert not (root / "notifications.jsonl").exists()
    assert [r["body"] for r in push.read_notifications()] == ["old", "new"]


# ── the producer door ───────────────────────────────────────────────────


class _NoBackend:
    def __init__(self, **kw):  # pragma: no cover
        raise AssertionError("notify must not touch the backend")


@pytest.fixture
def client(tmp_path, monkeypatch):
    wake = tmp_path / "wake-context.md"
    wake.write_text("ctx", encoding="utf-8")
    config = BrainConfig(sessions_root=tmp_path / "sessions", wake_context=wake)
    monkeypatch.setattr("prana.brain.api.load_brain_tokens",
                        lambda: {"prana": "tok-prana", "app": "tok-app"})
    monkeypatch.setattr(push, "_push_all", lambda *a, **k: {"sent": 0, "failed": 0, "dropped": 0})
    monkeypatch.setattr(push, "_telegram", lambda *a, **k: True)
    with TestClient(create_app(config, _NoBackend)) as c:
        yield c


def test_notify_door_is_prana_tier_and_validates(client):
    assert client.post("/v1/narada/notify", json={"body": "x"}).status_code == 401
    assert client.post("/v1/narada/notify", json={"body": "x"},
                       headers={"Authorization": "Bearer tok-app"}).status_code == 403
    h = {"Authorization": "Bearer tok-prana"}
    assert client.post("/v1/narada/notify", json={"body": "x", "level": "loud"},
                       headers=h).status_code == 400
    r = client.post("/v1/narada/notify", headers=h, json={
        "body": "job failed twice", "level": "error", "source": "job-watch/smriti-nightly",
        "category": "smriti-nightly/failed", "event_id": "jw-1"})
    assert r.status_code == 200
    assert "push" in r.json()["channels"]
    feed = client.get("/api/notifications", headers={"Authorization": "Bearer tok-app"}).json()
    assert feed[-1]["level"] == "error" and feed[-1]["source"] == "job-watch/smriti-nightly"


# ── review fixes ────────────────────────────────────────────────────────


def test_push_subsystem_failure_does_not_block_telegram():
    def boom(**_):
        raise RuntimeError("no pywebpush here")
    r = push.notify(body="down", level="error", source="host",
                    webpush_fn=boom, telegram_fn=lambda _t: True)
    # webpush_fn is called per subscription; with none, force the init path:
    r = push.notify(body="down again", level="error", source="host", event_id="e-x",
                    webpush_fn=boom, telegram_fn=lambda _t: True)
    assert r["telegram"] is True


def test_failed_channel_is_retried_without_recount(monkeypatch):
    calls = {"tg": 0}

    def flaky(_t):
        calls["tg"] += 1
        return calls["tg"] > 1  # first attempt fails

    r1 = push.notify(body="pager", level="error", source="host", category="x/y",
                     event_id="e-retry", webpush_fn=lambda **_: None, telegram_fn=flaky)
    assert r1["telegram"] is False and r1["row"]["delivered"]["telegram"] is False
    r2 = push.notify(body="pager", level="error", source="host", category="x/y",
                     event_id="e-retry", webpush_fn=lambda **_: None, telegram_fn=flaky)
    assert r2["new"] is False and r2["channels"] == ["telegram"] and r2["telegram"] is True
    rows = push.read_notifications()
    assert len(rows) == 1 and rows[0]["count"] == 1
    r3 = push.notify(body="pager", level="error", source="host", category="x/y",
                     event_id="e-retry", webpush_fn=lambda **_: None, telegram_fn=flaky)
    assert r3["channels"] == []  # fully delivered: true no-op


def test_folded_episode_moves_to_the_end():
    _notify(body="a", level="warn", source="host", category="ep/a")
    _notify(body="b", level="warn", source="host", category="ep/b")
    _notify(body="a again", level="warn", source="host", category="ep/a")
    rows = push.read_notifications()
    assert [r["category"] for r in rows] == ["ep/b", "ep/a"]
    assert rows[-1]["count"] == 2


# ── recheck fixes ───────────────────────────────────────────────────────


def test_earlier_event_stays_retryable_after_folding():
    calls = {"n": 0}

    def flaky(_t):
        calls["n"] += 1
        return calls["n"] != 1  # only the very first attempt fails

    push.notify(body="e1", level="error", source="host", category="c/x",
                event_id="e1", webpush_fn=lambda **_: None, telegram_fn=flaky)
    push.notify(body="e2", level="error", source="host", category="c/x",
                event_id="e2", webpush_fn=lambda **_: None, telegram_fn=flaky)
    r = push.notify(body="e1", level="error", source="host", category="c/x",
                    event_id="e1", webpush_fn=lambda **_: None, telegram_fn=flaky)
    assert r["new"] is False and r["channels"] == ["telegram"] and r["telegram"] is True
    assert push.read_notifications()[-1]["count"] == 2  # no recount


def test_replay_never_pages_even_for_a_previously_failed_event():
    push.notify(body="old", level="emergency", source="deadman", event_id="d1",
                webpush_fn=lambda **_: None, telegram_fn=lambda _t: False)
    r = push.notify(body="old", level="emergency", source="deadman", event_id="d1",
                    replay=True, webpush_fn=lambda **_: None, telegram_fn=lambda _t: True)
    assert r["channels"] == [] and r["telegram"] is None


def test_interrupted_delivery_is_retryable(monkeypatch):
    # Simulate a crash after record() but before the sinks ran: the row
    # carries PENDING (None) outcomes, not "delivered".
    def crash(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(push, "_push_all", crash)
    with pytest.raises(KeyboardInterrupt):
        push.notify(body="x", level="error", source="host", event_id="i1",
                    telegram_fn=lambda _t: True)
    row = push.read_notifications()[-1]
    assert row["delivered"]["push"] is None and row["delivered"]["telegram"] is None
    monkeypatch.setattr(push, "_push_all", lambda *a, **k: {"sent": 1, "failed": 0, "dropped": 0})
    r = push.notify(body="x", level="error", source="host", event_id="i1",
                    telegram_fn=lambda _t: True)
    assert r["new"] is False and sorted(r["channels"]) == ["push", "telegram"]
    assert push.read_notifications()[-1]["delivered"] == {"push": True, "telegram": True}


def test_messages_never_fold_into_an_episode(monkeypatch):
    """Review: two messages six minutes apart in one category are two
    things Narada said, not a fault repeating."""
    monkeypatch.setattr(push, "_push_all", lambda *a, **k: {"sent": 0, "failed": 0, "dropped": 0})
    monkeypatch.setattr(push, "_telegram", lambda *a, **k: True)
    first = push.notify(body="Good morning", level="message", source="notify-suti",
                        category="morning", event_id="m-1")
    second = push.notify(body="One more thing", level="message", source="notify-suti",
                         category="morning", event_id="m-2")
    rows = push.read_notifications()
    assert [r["body"] for r in rows] == ["Good morning", "One more thing"]
    assert all(r["count"] == 1 and r["open"] is False for r in rows)
    assert "push" in second["channels"] and "telegram" in second["channels"]
    assert first["row"]["id"] != second["row"]["id"]
    # A message during an open error episode in the same category stays a message.
    push.notify(body="disk failing", level="error", source="host", category="disk", event_id="e-1")
    push.notify(body="I'm on it", level="message", source="host", category="disk", event_id="m-3")
    rows = push.read_notifications()
    assert [r["level"] for r in rows[-2:]] == ["error", "message"]
    assert rows[-2]["open"] is True and rows[-2]["count"] == 1


def test_push_payload_carries_the_row_identity(monkeypatch):
    seen = []
    monkeypatch.setattr(push, "_load_subs", lambda: {"ep": {"endpoint": "ep"}})
    monkeypatch.setattr(push, "_vapid_pem_path", lambda: Path("nope.pem"))

    def fake_webpush(**kw):
        seen.append(json.loads(kw["data"]))

    r = push.notify(body="Water!", level="emergency", source="narada", webpush_fn=fake_webpush)
    assert seen and seen[0]["id"] == r["row"]["id"] and seen[0]["at"] == r["row"]["at"]
    assert seen[0]["level"] == "emergency"


def test_episode_revision_is_monotonic_even_within_one_second(monkeypatch):
    monkeypatch.setattr(push, "_push_all", lambda *a, **k: {"sent": 0, "failed": 0, "dropped": 0})
    monkeypatch.setattr(push, "_telegram", lambda *a, **k: True)
    a = push.notify(body="disk 90%", level="warn", source="host", category="disk", event_id="d-1")
    b = push.notify(body="disk full", level="emergency", source="host", category="disk", event_id="d-2")
    assert a["row"]["id"] == b["row"]["id"]
    assert a["row"]["rev"] == 1 and b["row"]["rev"] == 2
    seen = []
    monkeypatch.setattr(push, "_load_subs", lambda: {"ep": {"endpoint": "ep"}})
    monkeypatch.setattr(push, "_vapid_pem_path", lambda: Path("nope.pem"))
    monkeypatch.setattr(push, "_push_all", push.__dict__["_push_all"])  # restore the real one
    push.notify(body="disk on fire", level="emergency", source="host", category="disk", event_id="d-3",
                webpush_fn=lambda **kw: seen.append(json.loads(kw["data"])))
    assert seen[-1]["rev"] == 3
