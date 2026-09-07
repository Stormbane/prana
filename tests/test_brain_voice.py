"""Phone-app voice endpoints: token mint, session shape, dispatch
guardrails, and auth. Mirrors akhada's proven test surface for the
same flow; no LiveKit, no network — the twirp opener is injected."""

from __future__ import annotations

import base64
import json

import pytest
from starlette.testclient import TestClient

from prana.brain import voice
from prana.brain.api import create_app
from prana.brain.config import BrainConfig


def _b64pad(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ── token mint ──────────────────────────────────────────────────────────


def test_mint_token_payload_and_signature():
    tok = voice.mint_token("key1", "secret1", "suti-phone",
                           {"room": "narada-phone-x", "roomJoin": True},
                           ttl_s=600, now=1_000_000)
    header, payload, _sig = tok.split(".")
    assert json.loads(_b64pad(header)) == {"alg": "HS256", "typ": "JWT"}
    claims = json.loads(_b64pad(payload))
    assert claims["iss"] == "key1"
    assert claims["sub"] == "suti-phone"
    assert claims["exp"] == 1_000_000 + 600
    assert claims["video"]["room"] == "narada-phone-x"


def test_phone_session_shape():
    env = {"LIVEKIT_API_KEY": "k", "LIVEKIT_API_SECRET": "s"}
    info = voice.phone_session(env)
    assert info["room"].startswith(voice.ROOM_PREFIX)
    assert info["identity"] == voice.PHONE_IDENTITY
    assert info["agent"] == voice.AGENT_NAME
    assert info["token"].count(".") == 2
    # Distinct rooms per call — one room per talk-press.
    assert voice.phone_session(env)["room"] != info["room"]


def test_phone_session_requires_credentials():
    with pytest.raises(RuntimeError):
        voice.phone_session({})


# ── dispatch guardrails ─────────────────────────────────────────────────


def test_dispatch_refuses_foreign_rooms():
    with pytest.raises(ValueError):
        voice.dispatch_agent("narada-body", env={
            "LIVEKIT_API_KEY": "k", "LIVEKIT_API_SECRET": "s"})


def test_dispatch_posts_twirp_with_admin_token():
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"agent_dispatches": []}'

    def opener(req, timeout):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        seen["body"] = json.loads(req.data.decode())
        return _Resp()

    out = voice.dispatch_agent(
        "narada-phone-1-ab", opener=opener,
        env={"LIVEKIT_API_KEY": "k", "LIVEKIT_API_SECRET": "s",
             "LIVEKIT_URL": "ws://127.0.0.1:7880"})
    assert out == {"agent_dispatches": []}
    assert seen["url"].startswith("http://127.0.0.1:7880/twirp/")
    assert seen["auth"].startswith("Bearer ")
    assert seen["body"]["room"] == "narada-phone-1-ab"
    assert seen["body"]["agent_name"] == voice.AGENT_NAME


# ── endpoints ───────────────────────────────────────────────────────────


class _NoBackend:
    def __init__(self, **kw):  # pragma: no cover - never started here
        raise AssertionError("voice endpoints must not touch the backend")


@pytest.fixture
def client(tmp_path, monkeypatch):
    wake = tmp_path / "wake-context.md"
    wake.write_text("test wake context", encoding="utf-8")
    config = BrainConfig(sessions_root=tmp_path / "sessions",
                        wake_context=wake)
    tokens = {"prana": "tok-prana", "app": "tok-app"}
    monkeypatch.setattr("prana.brain.api.load_brain_tokens", lambda: tokens)
    app = create_app(config, _NoBackend)
    with TestClient(app) as c:
        yield c


def test_session_requires_auth(client):
    assert client.get("/api/voice/session").status_code == 401


def test_dispatch_requires_auth(client):
    assert client.post("/api/voice/dispatch",
                       json={"room": "narada-phone-1"}).status_code == 401


def test_session_returns_info(client, monkeypatch):
    monkeypatch.setattr(voice, "load_livekit_env", lambda path=None: {
        "LIVEKIT_API_KEY": "k", "LIVEKIT_API_SECRET": "s"})
    res = client.get("/api/voice/session",
                     headers={"Authorization": "Bearer tok-app"})
    assert res.status_code == 200
    info = res.json()
    assert set(info) == {"room", "token", "identity", "url", "agent"}
    assert info["room"].startswith(voice.ROOM_PREFIX)


def test_session_503_without_credentials(client, monkeypatch):
    monkeypatch.setattr(voice, "load_livekit_env", lambda path=None: {})
    res = client.get("/api/voice/session",
                     headers={"Authorization": "Bearer tok-app"})
    assert res.status_code == 503


def test_dispatch_refuses_foreign_room_403(client):
    res = client.post("/api/voice/dispatch", json={"room": "narada-body"},
                      headers={"Authorization": "Bearer tok-app"})
    assert res.status_code == 403


def test_dispatch_400_on_missing_room(client):
    res = client.post("/api/voice/dispatch", json={},
                      headers={"Authorization": "Bearer tok-app"})
    assert res.status_code == 400
