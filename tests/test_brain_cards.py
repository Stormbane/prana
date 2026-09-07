"""Auto-cards (plan §T4): minting from tool results, and the wire —
card chunks in the stream, narada_cards on non-stream bodies."""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from prana.brain.api import create_app
from prana.brain.cards import CardEvent, card_for, interesting
from prana.brain.config import BrainConfig


def test_interesting_matches_bare_and_prefixed_names():
    assert interesting("mcp__akhada__log_meal") and interesting("log_meal")
    assert not interesting("mcp__smriti__smriti_read")


def test_meal_card_from_log_meal_result():
    # akhada's REAL write result: item_ids + meal_totals/day_totals; the
    # items and label live in the tool INPUT.
    res = {"ok": True, "meal_id": 7, "item_ids": [1], "day": "2026-09-08",
           "meal_totals": {"calories": 300, "protein_g": 40},
           "day_totals": {"calories": 1200, "protein_g": 90}}
    tool_input = {"label": "lunch",
                  "items": [{"name": "chicken", "calories": 300,
                             "calories_low": 250, "calories_high": 350}]}
    spec = card_for("mcp__akhada__log_meal", tool_input, json.dumps(res))
    assert spec["kind"] == "kv" and spec["caption"] == "Logged lunch"
    assert spec["data"]["chicken"] == "250–350 kcal"
    assert spec["data"]["meal calories"] == "300 kcal"
    assert spec["data"]["day calories"] == "1200 kcal"
    assert spec["v"] == 1 and spec["card_id"].startswith("auto-")


def test_summary_and_recent_cards():
    day = card_for("get_today_summary", {}, json.dumps({
        "ok": True, "day": "2026-09-08", "totals": {"calories": 674, "protein_g": 40},
        "meals": [{"voided_at": None}, {"voided_at": "x"}], "remaining": {"protein_g": 140}}))
    assert day["caption"] == "2026-09-08 so far" and day["data"]["meals"] == "1"
    assert day["data"]["protein to go"] == "140 g"
    week = card_for("get_recent", {"days": 7}, json.dumps({
        "ok": True, "start": "2026-09-01", "end": "2026-09-07",
        "averages": {"all_days": {"calories": 900, "protein_g": 60},
                     "logged_days_only": {"calories": 1800.4, "protein_g": 120}},
        "days": [{"logged": True}, {"logged": False}]}))
    assert week["data"]["days logged"] == "1"
    assert week["data"]["avg calories (logged days)"] == "1800 kcal"


def test_uninteresting_or_failed_results_mint_nothing():
    assert card_for("smriti_read", {}, "{}") is None
    assert card_for("log_meal", {}, json.dumps({"ok": False})) is None
    assert card_for("log_meal", {}, "not json") is None


# ── the wire ────────────────────────────────────────────────────────────


class CardBackend:
    """Yields a text chunk, a card, then more text."""

    def __init__(self, **kw):
        pass

    async def start(self):
        pass

    async def run_turn(self, prompt):
        yield "Logging "
        yield CardEvent({"card_id": "auto-1", "kind": "kv", "v": 1, "as_of": "t",
                         "caption": "Logged lunch", "data": {"chicken": "300 kcal"}})
        yield "done."

    async def cancel(self):
        pass

    async def close(self):
        pass

    @property
    def native_session_id(self):
        return None


@pytest.fixture
def client(tmp_path, monkeypatch):
    wake = tmp_path / "wake-context.md"
    wake.write_text("ctx", encoding="utf-8")
    config = BrainConfig(sessions_root=tmp_path / "sessions", wake_context=wake,
                        turn_deadline_s=5.0)
    monkeypatch.setattr("prana.brain.api.load_brain_tokens", lambda: {"app": "tok-app"})
    with TestClient(create_app(config, CardBackend)) as c:
        yield c


def _body(**kw):
    return {"model": "sonnet", "messages": [{"role": "user", "content": "log lunch"}],
            "narada": {"session_id": "s-cards", "request_id": "r1"}, **kw}


def test_stream_carries_card_chunks_between_text(client):
    with client.stream("POST", "/v1/chat/completions", json=_body(stream=True),
                       headers={"Authorization": "Bearer tok-app"}) as r:
        events = [json.loads(line[6:]) for line in r.iter_lines()
                  if line.startswith("data: ") and line != "data: [DONE]"]
    texts = "".join(e["choices"][0]["delta"].get("content", "") for e in events)
    cards = [e["narada_card"] for e in events if "narada_card" in e]
    assert texts == "Logging done."
    assert cards == [{"card_id": "auto-1", "kind": "kv", "v": 1, "as_of": "t",
                      "caption": "Logged lunch", "data": {"chicken": "300 kcal"}}]


def test_non_stream_body_lists_cards(client):
    r = client.post("/v1/chat/completions", json=_body(stream=False),
                    headers={"Authorization": "Bearer tok-app"})
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "Logging done."
    assert r.json()["narada_cards"][0]["caption"] == "Logged lunch"


def test_replay_of_the_same_request_id_carries_the_cards(client):
    h = {"Authorization": "Bearer tok-app"}
    first = client.post("/v1/chat/completions", json=_body(stream=False), headers=h)
    assert first.json()["narada_cards"][0]["card_id"] == "auto-1"
    # Same request_id again: replay-not-rerun, cards included, ids preserved.
    again = client.post("/v1/chat/completions", json=_body(stream=False), headers=h)
    assert again.status_code == 200
    assert again.json()["narada_cards"] == first.json()["narada_cards"]
    with client.stream("POST", "/v1/chat/completions", json=_body(stream=True), headers=h) as r:
        events = [json.loads(line[6:]) for line in r.iter_lines()
                  if line.startswith("data: ") and line != "data: [DONE]"]
    assert [e["narada_card"]["card_id"] for e in events if "narada_card" in e] == ["auto-1"]
