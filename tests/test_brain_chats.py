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
    # last write wins, whole document
    _put(client, "conv-1", {**body, "title": "Back day (renamed)", "messages": body["messages"][:1]})
    assert client.get("/api/chats/conv-1", headers={"Authorization": "Bearer tok-app"}).json()["title"] == "Back day (renamed)"
    assert client.get("/api/chats", headers={"Authorization": "Bearer tok-app"}).json()[0]["messages"] == 1


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
