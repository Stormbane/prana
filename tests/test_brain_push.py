"""Web Push endpoints and store: subscribe validation, tier-gated
send, expired-subscription pruning. No network — webpush is injected,
and the store lives in a temp HOME."""

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


def _sub(endpoint="https://push.example/ep1"):
    return {"endpoint": endpoint,
            "keys": {"p256dh": "BPub", "auth": "auth"}}


# ── store ───────────────────────────────────────────────────────────────


def test_subscribe_requires_https_endpoint_and_keys():
    with pytest.raises(ValueError):
        push.subscribe({"endpoint": "http://insecure/ep"})
    with pytest.raises(ValueError):
        push.subscribe({"endpoint": "https://push.example/ep"})
    assert push.subscribe(_sub()) == 1
    # Same endpoint re-subscribes in place, no duplicates.
    assert push.subscribe(_sub()) == 1


def test_unsubscribe():
    push.subscribe(_sub())
    assert push.unsubscribe("https://push.example/ep1") == 0


def test_send_prunes_expired(monkeypatch):
    from pywebpush import WebPushException

    push.subscribe(_sub("https://push.example/live"))
    push.subscribe(_sub("https://push.example/gone"))
    monkeypatch.setattr(push, "ensure_vapid", lambda: {
        "private_pem": "pem", "public_key": "pub"})

    class _Resp:
        status_code = 410

    def fake_webpush(subscription_info, **kw):
        if subscription_info["endpoint"].endswith("/gone"):
            raise WebPushException("gone", response=_Resp())

    out = push.send("t", "b", webpush_fn=fake_webpush)
    assert out == {"sent": 1, "failed": 0, "dropped": 1}
    # The expired subscription is gone from the store.
    assert push.subscribe(_sub("https://push.example/live")) == 1


# ── endpoints ───────────────────────────────────────────────────────────


class _NoBackend:
    def __init__(self, **kw):  # pragma: no cover
        raise AssertionError("push endpoints must not touch the backend")


@pytest.fixture
def client(tmp_path, monkeypatch):
    wake = tmp_path / "wake-context.md"
    wake.write_text("ctx", encoding="utf-8")
    config = BrainConfig(sessions_root=tmp_path / "sessions",
                        wake_context=wake)
    tokens = {"prana": "tok-prana", "app": "tok-app"}
    monkeypatch.setattr("prana.brain.api.load_brain_tokens", lambda: tokens)
    app = create_app(config, _NoBackend)
    with TestClient(app) as c:
        yield c


def test_subscribe_needs_auth(client):
    assert client.post("/api/push/subscribe", json=_sub()).status_code == 401


def test_app_tier_can_subscribe_but_not_send(client):
    headers = {"Authorization": "Bearer tok-app"}
    res = client.post("/api/push/subscribe", json=_sub(), headers=headers)
    assert res.status_code == 200
    assert res.json() == {"subscriptions": 1}
    res = client.post("/api/push/send", json={"body": "hi"}, headers=headers)
    assert res.status_code == 403


def test_prana_tier_send(client, monkeypatch):
    monkeypatch.setattr(push, "send",
                        lambda title, body, **kw: {"sent": 0, "failed": 0,
                                                   "dropped": 0})
    res = client.post("/api/push/send", json={"body": "hi"},
                      headers={"Authorization": "Bearer tok-prana"})
    assert res.status_code == 200
    assert res.json()["sent"] == 0


def test_send_400_on_empty_body(client):
    res = client.post("/api/push/send", json={},
                      headers={"Authorization": "Bearer tok-prana"})
    assert res.status_code == 400


def test_vapid_key_stable(client):
    headers = {"Authorization": "Bearer tok-app"}
    k1 = client.get("/api/push/vapid-key", headers=headers).json()["key"]
    k2 = client.get("/api/push/vapid-key", headers=headers).json()["key"]
    assert k1 == k2 and len(k1) > 40
