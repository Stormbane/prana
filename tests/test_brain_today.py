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


def test_followup_and_choice_taps(client):
    from akhada import followups
    from akhada.store.db import Store
    f = followups.ask_followup(Store(), question="Energy?", options=["Low", "OK", "High"], about="energy")
    r = client.post("/api/today/answer", headers=H, json={
        "identity": f"followup:{f['followup_id']}", "kind": "choice", "event_id": "fu-1",
        "payload": {"choice": "High"}}).json()
    assert r["result"]["ok"]
    card = next(c for c in r["today"]["cards"] if c["data"]["identity"] == f"followup:{f['followup_id']}")
    assert card["data"]["answer"] == "High"


def test_ask_followup_tool_result_becomes_the_chat_card():
    import json
    from prana.brain.cards import card_for
    spec = {"card_id": "followup-7", "kind": "ask", "v": 1, "rev": 1, "caption": "Energy?",
            "data": {"ask_kind": "choice"}}
    out = card_for("mcp__akhada__ask_followup", {}, json.dumps({"ok": True, "card": spec}))
    assert out == spec


def test_a_tap_delivered_late_reports_on_the_day_it_was_made(client):
    from akhada import clock
    clock.set_fake_now(datetime(2026, 10, 13, 5, 0, tzinfo=clock.TZ))   # delivered next morning
    r = client.post("/api/today/answer", headers=H, json={
        "identity": "check", "kind": "check", "event_id": "late-1", "expected_revision": 0,
        "payload": {"food": "on_track", "trained": "rest", "at": "2026-10-12T23:58:00+10:00"}}).json()
    assert r["result"]["ok"] and r["result"]["day"] == "2026-10-12"


def test_a_tap_on_yesterdays_page_answers_yesterdays_check_until_noon(client):
    """Redesign L2a (Codex R1): the day page names the check it answers."""
    from akhada import clock
    clock.set_fake_now(datetime(2026, 10, 13, 9, 0, tzinfo=clock.TZ))
    r = client.post("/api/today/answer", headers=H, json={
        "identity": "check", "kind": "check", "event_id": "late-1", "day": "2026-10-12",
        "payload": {"food": "so_so", "trained": "rest"}}).json()
    assert r["result"]["ok"] and r["result"]["day"] == "2026-10-12"
    clock.set_fake_now(datetime(2026, 10, 13, 12, 5, tzinfo=clock.TZ))
    r = client.post("/api/today/answer", headers=H, json={
        "identity": "check", "kind": "check", "event_id": "late-2", "day": "2026-10-12",
        "payload": {"food": "so_so", "trained": "rest"}}).json()
    assert r["result"]["ok"] is False and r["result"]["closed"] is True


def test_other_asks_aimed_at_an_older_page_are_refused(client):
    from akhada import clock
    clock.set_fake_now(datetime(2026, 10, 13, 9, 0, tzinfo=clock.TZ))
    r = client.post("/api/today/answer", headers=H, json={
        "identity": "vital:weight", "kind": "number", "event_id": "w-old", "day": "2026-10-12",
        "payload": {"value": 80.4, "at": "2026-10-13T09:00:00+10:00"}}).json()
    assert r["result"]["ok"] is False and "carried over to today" in r["result"]["error"]
    r = client.post("/api/today/answer", headers=H, json={
        "identity": "vital:weight", "kind": "number", "event_id": "w-new", "day": "2026-10-13",
        "payload": {"value": 80.4}}).json()
    assert r["result"]["ok"]


def test_an_offline_answer_delivered_after_the_turn_keeps_its_day(client):
    from akhada import clock
    clock.set_fake_now(datetime(2026, 10, 13, 5, 0, tzinfo=clock.TZ))
    r = client.post("/api/today/answer", headers=H, json={
        "identity": "vital:weight", "kind": "number", "event_id": "w-queued", "day": "2026-10-12",
        "payload": {"value": 80.4, "at": "2026-10-12T23:58:00+10:00"}}).json()
    assert r["result"]["ok"]


def test_a_tap_without_its_own_time_reaches_akhadas_replay(client):
    """Review: a committed answer retried after 04:00 without payload.at
    must replay, not be refused by the older-page guard."""
    from akhada import clock
    clock.set_fake_now(datetime(2026, 10, 13, 3, 0, tzinfo=clock.TZ))
    body = {"identity": "vital:weight", "kind": "number", "event_id": "w-retry", "day": "2026-10-12",
            "payload": {"value": 80.4}}
    assert client.post("/api/today/answer", headers=H, json=body).json()["result"]["ok"]
    clock.set_fake_now(datetime(2026, 10, 13, 5, 0, tzinfo=clock.TZ))
    again = client.post("/api/today/answer", headers=H, json=body).json()["result"]
    assert not again.get("closed")


def test_a_bad_time_is_a_bad_request(client):
    r = client.post("/api/today/answer", headers=H, json={
        "identity": "vital:weight", "kind": "number", "event_id": "w-bad", "day": "2026-10-12",
        "payload": {"value": 80.4, "at": "not a time"}})
    assert r.status_code == 400


def test_a_new_answer_aimed_at_an_older_page_without_a_time_is_still_refused(client):
    """Recheck: the replay exception must not let a NEW stale-page answer
    write to today."""
    from akhada import clock
    clock.set_fake_now(datetime(2026, 10, 13, 9, 0, tzinfo=clock.TZ))
    r = client.post("/api/today/answer", headers=H, json={
        "identity": "vital:weight", "kind": "number", "event_id": "w-new-old", "day": "2026-10-12",
        "payload": {"value": 80.4}}).json()
    assert r["result"]["ok"] is False and r["result"]["closed"] is True
