"""The chat mirror: the phone's own transcripts, kept here so a chat
typed while it could not connect can still be retrieved (Suti
2026-09-21)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from prana.brain.api import create_app
from prana.brain.config import BrainConfig


class _Backend:
    def __init__(self, **kw):
        pass

    async def start(self):
        pass

    async def close(self):
        pass

    async def run_turn(self, prompt):
        yield "ok"

    async def cancel(self):
        pass

    @property
    def native_session_id(self):
        return None


@pytest.fixture
def brain(tmp_path, monkeypatch):
    wake = tmp_path / "wake.md"
    wake.write_text("wake", encoding="utf-8")
    config = BrainConfig(sessions_root=tmp_path / "sessions", chats_root=tmp_path / "chats",
                         wake_context=wake)
    monkeypatch.setattr("prana.brain.api.load_brain_tokens",
                        lambda: {"prana": "tok-prana", "app": "tok-app", "voice": "tok-voice"})
    with TestClient(create_app(config, _Backend)) as client:
        yield client, config


def _put(client, chat_id, body, token="tok-app"):
    return client.put(f"/api/chats/{chat_id}", content=json.dumps(body),
                      headers={"Authorization": f"Bearer {token}", "content-type": "application/json"})


def test_a_mirrored_chat_lands_on_disk_and_reads_back(brain):
    client, config = brain
    body = {"title": "Back day", "sessionId": "s-1", "updatedAt": 1700,
            "messages": [{"id": "m1", "role": "user", "text": "lat pulldown 8 at 60", "status": "complete"},
                         {"id": "m2", "role": "assistant", "text": "", "status": "error",
                          "errorDetail": "Failed to fetch"}]}
    r = _put(client, "conv-1", body)
    assert r.status_code == 200 and r.json()["messages"] == 2
    doc = json.loads((config.chats_root / "app" / "conv-1.json").read_text(encoding="utf-8"))
    assert doc["title"] == "Back day" and doc["tier"] == "app" and doc["mirrored_at"]
    assert doc["messages"][0]["text"] == "lat pulldown 8 at 60"
    got = client.get("/api/chats/conv-1", headers={"Authorization": "Bearer tok-app"})
    assert got.status_code == 200 and got.json()["messages"][1]["status"] == "error"
    listed = client.get("/api/chats", headers={"Authorization": "Bearer tok-app"}).json()
    assert listed == [{"id": "conv-1", "title": "Back day", "updatedAt": 1700,
                       "mirrored_at": doc["mirrored_at"], "messages": 2}]
    # the document's fields follow the last write; messages MERGE by id
    # (redesign L2b): a shorter phone copy never erases what the brain has
    _put(client, "conv-1", {**body, "title": "Back day (renamed)", "messages": body["messages"][:1]})
    assert client.get("/api/chats/conv-1", headers={"Authorization": "Bearer tok-app"}).json()["title"] == "Back day (renamed)"
    assert client.get("/api/chats", headers={"Authorization": "Bearer tok-app"}).json()[0]["messages"] == 2


def test_a_long_chat_keeps_its_history_when_the_phone_holds_only_the_end(brain):
    """Codex R3: the phone keeps its last 60 messages; the brain must keep
    the rest, take the phone's edits in place, and add what's new."""
    client, _ = brain
    msg = lambda i, text=None: {"id": f"m{i}", "role": "user", "text": text or f"line {i}", "status": "complete"}
    _put(client, "long", {"title": "Long", "messages": [msg(i) for i in range(100)]})
    # the phone now holds only 60..99, has edited m95, and added m100
    phone = [msg(i) for i in range(60, 100)]
    phone[35] = msg(95, "edited")
    _put(client, "long", {"title": "Long", "messages": phone + [msg(100)]})
    doc = client.get("/api/chats/long", headers={"Authorization": "Bearer tok-app"}).json()
    ids = [m["id"] for m in doc["messages"]]
    assert ids == [f"m{i}" for i in range(101)]
    assert doc["messages"][95]["text"] == "edited"
    # sending the same thing again changes nothing
    _put(client, "long", {"title": "Long", "messages": phone + [msg(100)]})
    assert len(client.get("/api/chats/long", headers={"Authorization": "Bearer tok-app"}).json()["messages"]) == 101


def test_tiers_are_separate_and_ids_and_bodies_are_checked(brain):
    client, config = brain
    body = {"messages": []}
    assert _put(client, "conv-2", body, token="tok-voice").status_code == 200
    assert (config.chats_root / "voice" / "conv-2.json").is_file()
    assert client.get("/api/chats/conv-2", headers={"Authorization": "Bearer tok-app"}).status_code == 404
    assert _put(client, "../escape", body).status_code in (400, 404)
    assert _put(client, "bad id", body).status_code in (400, 404)
    assert _put(client, "conv-3", {"messages": "no"}).status_code == 400
    big = {"messages": [{"id": "m", "role": "user", "text": "x" * 1_100_000, "status": "complete"}]}
    assert _put(client, "conv-4", big).status_code == 413
    assert client.put("/api/chats/conv-5", content="{}").status_code == 401



def test_odd_ids_never_break_the_mirror(brain):
    """Review: a non-string id (from an old client, or already on disk) is
    kept by value, never a 500, and never blocks later writes."""
    client, _ = brain
    r = _put(client, "odd", {"title": "Odd", "messages": [{"id": ["m1"], "text": "x"}, {"text": "no id"}]})
    assert r.status_code == 200
    r = _put(client, "odd", {"title": "Odd", "messages": [{"id": "m2", "text": "fine"}, {"text": "no id"}]})
    assert r.status_code == 200 and r.json()["messages"] == 3


def test_a_big_history_merges_quickly(brain):
    import time
    client, _ = brain
    big = [{"id": f"m{i}", "role": "user", "text": "x"} for i in range(5000)]
    start = time.perf_counter()
    assert _put(client, "big", {"title": "Big", "messages": big}).status_code == 200
    assert time.perf_counter() - start < 1.0
