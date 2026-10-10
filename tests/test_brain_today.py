"""Questling v1 on the brain: /api/today and tap answers (akhada spec §3)."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from prana.brain.api import create_app
from prana.brain.config import BrainConfig

akhada = pytest.importorskip("akhada")


class _NoBackend:
    def __init__(self, **kw):  # pragma: no cover
        raise AssertionError("today must not touch the backend")


@pytest.fixture
def client(tmp_path, monkeypatch):
    from akhada import clock
    from akhada.seed import seed
    from akhada.store.db import Store

    monkeypatch.setenv("AKHADA_DB", str(tmp_path / "akhada.db"))
    seed(Store(), write_profile=False)
    clock.set_fake_now(datetime(2026, 10, 12, 19, 0, tzinfo=clock.TZ))
    wake = tmp_path / "wake-context.md"
    wake.write_text("ctx", encoding="utf-8")
    config = BrainConfig(sessions_root=tmp_path / "sessions", wake_context=wake)
    monkeypatch.setattr("prana.brain.api.load_brain_tokens",
                        lambda: {"prana": "tok-prana", "app": "tok-app"})
    with TestClient(create_app(config, _NoBackend)) as c:
        yield c
    clock.set_fake_now(None)


H = {"Authorization": "Bearer tok-app"}


def test_today_needs_a_token_and_returns_cards(client):
    assert client.get("/api/today").status_code == 401
    t = client.get("/api/today", headers=H).json()
    assert t["day"] == "2026-10-12"
    ids = [c["data"]["identity"] for c in t["cards"]]
    assert ids[0] == "check" and "vital:weight" in ids


def test_a_tap_commits_once_and_returns_fresh_cards(client):
    body = {"identity": "check", "kind": "check", "event_id": "tap-1", "expected_revision": 0,
            "payload": {"food": "so_so", "trained": "yes", "mood": 4}}
    r = client.post("/api/today/answer", json=body, headers=H).json()
    assert r["result"]["ok"] and r["result"]["revision"] == 1
    check = next(c for c in r["today"]["cards"] if c["data"]["identity"] == "check")
    assert check["data"]["status"] == "answered"
    again = client.post("/api/today/answer", json=body, headers=H).json()
    assert again["result"].get("duplicate") is True
    assert again["today"]["mana"]["today"] == r["today"]["mana"]["today"]


def test_number_and_skip_taps(client):
    r = client.post("/api/today/answer", headers=H, json={
        "identity": "vital:weight", "kind": "number", "event_id": "w-1",
        "payload": {"value": 80.4}}).json()
    w = next(c for c in r["today"]["cards"] if c["data"]["identity"] == "vital:weight")
    assert w["data"]["status"] == "answered" and w["data"]["answer"] == "80.4 kg"
    r = client.post("/api/today/answer", headers=H, json={
        "identity": "check", "kind": "skip", "event_id": "s-1"}).json()
    assert r["result"]["ok"] is False and "can't be skipped" in r["result"]["error"]


def test_a_tap_without_event_id_is_refused(client):
    r = client.post("/api/today/answer", headers=H, json={
        "identity": "check", "kind": "check", "payload": {"food": "so_so", "trained": "no"}})
    assert r.status_code == 400
