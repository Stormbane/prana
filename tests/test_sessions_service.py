"""Persistent service round-trip: auth, dispatch, client surface."""

from __future__ import annotations

import json
import socket
import threading

import pytest

import prana.sessions.manager as manager_mod
from prana.sessions.manager import ManagerConfig, SessionManager
from prana.sessions.registry import SessionState
from prana.sessions.service import (
    HOST,
    ServiceClient,
    ServiceUnavailable,
    SessionService,
)


class FakeProc:
    _pid = 95000

    def __init__(self):
        FakeProc._pid += 1
        self.pid = FakeProc._pid
        self.killed = False

    def start(self):
        pass

    def kill(self):
        self.killed = True


@pytest.fixture
def service(tmp_path, monkeypatch):
    def fake_spawn(prompt, cwd, on_event, *, resume_session_id=None):
        return FakeProc()

    for provider in ("claude", "codex", "kimi"):
        monkeypatch.setitem(manager_mod.SPAWNERS, provider, fake_spawn)
    mgr = SessionManager(ManagerConfig(db_path=tmp_path / "s.db"))
    svc = SessionService(manager=mgr, port=0, token="test-token")
    thread = threading.Thread(target=svc.serve_forever, daemon=True)
    thread.start()
    yield svc
    svc.shutdown()


def _client(svc) -> ServiceClient:
    return ServiceClient(port=svc.port, token="test-token")


def test_ping_and_spawn_roundtrip(service):
    client = _client(service)
    assert client.ping() is True
    sess = client.spawn("claude", r"C:\p", "build the thing")
    assert sess["state"] == "running"
    listed = client.list_sessions()
    assert [s["id"] for s in listed] == [sess["id"]]
    assert client.get(sess["id"])["provider"] == "claude"
    cancelled = client.cancel(sess["id"])
    assert cancelled["state"] == "killed"


def test_bad_auth_rejected(service):
    client = ServiceClient(port=service.port, token="wrong")
    with pytest.raises(RuntimeError, match="bad auth"):
        client.ping()


def test_unknown_method_rejected(service):
    payload = json.dumps(
        {"auth": "test-token", "method": "drop_tables", "params": {}}
    ).encode() + b"\n"
    with socket.create_connection((HOST, service.port), timeout=5) as sock:
        sock.sendall(payload)
        reply = json.loads(sock.makefile().readline())
    assert reply["ok"] is False


def test_service_unreachable_raises(tmp_path):
    client = ServiceClient(port=1, token="x")  # nothing listens on port 1
    with pytest.raises(ServiceUnavailable):
        client.ping()


def test_error_propagates_not_crashes(service):
    client = _client(service)
    with pytest.raises(RuntimeError, match="no session"):
        client.get("nonexistent-id")
    # service still alive afterwards
    assert client.ping() is True
