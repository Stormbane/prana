"""Brief mode (Suti on the phone 2026-09-08): a voice pref flipped from
Settings or by voice; personal tier only; the worker reads it per call."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from prana.brain.api import create_app
from prana.brain.config import BrainConfig


def _names(tools) -> set[str]:
    out = set()
    for t in tools:
        info = getattr(t, "info", None) or getattr(t, "_info", None)
        out.add(getattr(info, "name", None) or getattr(t, "__name__", None))
    return out


def _build(tier: str, tmp_path: Path):
    from prana.sessions.escalate import ProposalQueue
    from prana.sessions.service import ServiceClient
    from prana.voice.tools import build_voice_tools

    return build_voice_tools(
        client=ServiceClient(port=1, token="unused"),
        proposals=ProposalQueue(tmp_path / "s.db"), tier=tier)


def test_brief_mode_is_personal_only(tmp_path):
    assert "brief_mode" not in _names(_build("shareable", tmp_path))
    assert "brief_mode" in _names(_build("personal", tmp_path))


def test_brief_mode_tool_persists_the_pref_and_steers_now(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    tools = {n: t for t in _build("personal", tmp_path)
             for n in [next(iter(_names([t])))]}
    tool = tools["brief_mode"]
    on = asyncio.run(tool(on=True))
    assert on["ok"] and on["brevity"] == "brief" and "ON" in on["instruction"]
    prefs = json.loads((tmp_path / ".narada" / "brain" / "voice-prefs.json").read_text())
    assert prefs["brevity"] == "brief"
    off = asyncio.run(tool(on=False))
    assert off["brevity"] == "normal"
    assert json.loads((tmp_path / ".narada" / "brain" / "voice-prefs.json").read_text())["brevity"] == "normal"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    wake = tmp_path / "wake-context.md"
    wake.write_text("ctx", encoding="utf-8")
    config = BrainConfig(sessions_root=tmp_path / "sessions", wake_context=wake)
    monkeypatch.setattr("prana.brain.api.load_brain_tokens", lambda: {"app": "tok-app"})

    class Backend:
        def __init__(self, **kw): ...
        async def start(self): ...
        async def run_turn(self, prompt):
            yield "hi"
        async def cancel(self): ...
        async def close(self): ...
        native_session_id = None

    with TestClient(create_app(config, Backend)) as c:
        yield c


def test_prefs_endpoint_carries_brevity(client):
    h = {"Authorization": "Bearer tok-app"}
    assert client.get("/api/voice/prefs", headers=h).json()["brevity"] == "normal"
    r = client.post("/api/voice/prefs", json={"brevity": "brief"}, headers=h)
    assert r.status_code == 200
    assert client.get("/api/voice/prefs", headers=h).json()["brevity"] == "brief"
    assert client.post("/api/voice/prefs", json={"brevity": "terse"}, headers=h).status_code == 400


def test_update_prefs_is_locked_atomic_and_refuses_a_malformed_file(tmp_path):
    from prana.voice.prefs import PrefsError, read_prefs, update_prefs

    path = tmp_path / "voice-prefs.json"
    update_prefs({"vad_threshold": 0.9}, path)
    assert read_prefs(path) == {"vad_threshold": 0.9}
    # Merge keeps other keys (the lost-update the review reproduced).
    assert update_prefs({"brevity": "brief"}, path) == {"vad_threshold": 0.9, "brevity": "brief"}
    assert not path.with_suffix(".json.tmp").exists()
    assert not path.with_suffix(".json.lock").exists()
    # A held lock refuses rather than racing.
    lock = path.with_suffix(".json.lock")
    lock.write_text("held", encoding="utf-8")
    with pytest.raises(PrefsError):
        update_prefs({"brevity": "normal"}, path)
    lock.unlink()
    # A malformed file is never overwritten by a writer; readers degrade.
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(PrefsError):
        update_prefs({"brevity": "normal"}, path)
    assert path.read_text(encoding="utf-8") == "{not json"
    assert read_prefs(path) == {}


def test_a_held_lock_surfaces_as_503_not_a_silent_success(client, tmp_path):
    from prana.voice.prefs import prefs_path

    h = {"Authorization": "Bearer tok-app"}
    lock = prefs_path().with_suffix(".json.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("held", encoding="utf-8")
    try:
        assert client.post("/api/voice/prefs", json={"brevity": "brief"}, headers=h).status_code == 503
    finally:
        lock.unlink()
