"""Narada reads the phone's mirrored chats on request (Suti 2026-09-21)."""

from __future__ import annotations

import json


def test_list_and_read_the_mirror_including_failed_turns(tmp_path, monkeypatch):
    monkeypatch.setenv("NARADA_BRAIN_CHATS_ROOT", str(tmp_path))
    from prana.chats import mcp as chats

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "c1.json").write_text(json.dumps({
        "id": "c1", "title": "Hello", "updatedAt": 200, "mirrored_at": "t",
        "messages": [
            {"id": "u1", "role": "user", "text": "40kg x 8", "status": "complete"},
            {"id": "a1", "role": "assistant", "text": "Logged", "status": "complete"},
            {"id": "u2", "role": "user", "text": "core machine 20kg x 20", "status": "complete"},
            {"id": "a2", "role": "assistant", "text": "", "status": "error", "errorDetail": "Failed to fetch"},
        ]}), encoding="utf-8")
    (tmp_path / "app" / "c2.json").write_text(json.dumps({"id": "c2", "title": "Older", "updatedAt": 100, "messages": []}),
                                              encoding="utf-8")
    listed = json.loads(chats.list_chats())
    assert [c["id"] for c in listed] == ["c1", "c2"]
    assert listed[0]["failed_turns"] == 1 and listed[0]["messages"] == 4
    full = json.loads(chats.read_chat("c1", last_n=2))
    assert full["ok"] and [m["text"] for m in full["messages"]] == ["core machine 20kg x 20", ""]
    failed = json.loads(chats.read_chat("c1", failed_only=True))
    assert failed["failed"] == [{"user": "core machine 20kg x 20", "error": "Failed to fetch"}]
    assert json.loads(chats.read_chat("../c1"))["ok"] is False
    assert json.loads(chats.read_chat("nope"))["ok"] is False
