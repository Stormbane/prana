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
