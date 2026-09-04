"""Brain-server §1a contract tests: auth, session isolation, the
idempotency state machine, one-active-turn, wire shape, disconnect
semantics. Runs against a FakeBackend — no SDK, no network."""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from starlette.testclient import TestClient

from prana.brain.api import create_app
from prana.brain.config import BrainConfig
from prana.brain.turns import TurnStore, fingerprint


class FakeBackend:
    """Yields canned chunks; records lifecycle calls."""

    instances: list["FakeBackend"] = []

    def __init__(self, *, model, system_append, mcp_servers,
                 max_tool_iterations, cwd, resume=None,
                 chunks=("Hello ", "from Narada"), delay=0.0):
        self.mcp_servers = mcp_servers
        self.resume = resume
        self.chunks = list(chunks)
        self.delay = delay
        self.started = False
        self.closed = False
        self.cancelled = False
        FakeBackend.instances.append(self)

    async def start(self):
        self.started = True

    async def run_turn(self, prompt):
        for c in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.cancelled:
                return
            yield c

    async def cancel(self):
        self.cancelled = True

    async def close(self):
        self.closed = True

    @property
    def native_session_id(self):
        return "fake-native-1"


@pytest.fixture
def brain(tmp_path, monkeypatch):
    wake = tmp_path / "wake-context.md"
    wake.write_text("test wake context", encoding="utf-8")
    config = BrainConfig(
        sessions_root=tmp_path / "sessions",
        wake_context=wake,
        turn_deadline_s=5.0,
    )
    tokens = {"prana": "tok-prana", "app": "tok-app", "voice": "tok-voice"}
    monkeypatch.setattr("prana.brain.api.load_brain_tokens", lambda: tokens)
    FakeBackend.instances = []
    app = create_app(config, FakeBackend)
    with TestClient(app) as client:
        yield client, config


def _post(client, token="tok-prana", **kw):
    body = {"model": "sonnet",
            "messages": [{"role": "user", "content": "hi"}]}
    body.update(kw)
    return client.post(
        "/v1/chat/completions", json=body,
        headers={"Authorization": f"Bearer {token}"} if token else {})


# ── auth ────────────────────────────────────────────────────────────────


def test_no_token_is_401(brain):
    client, _ = brain
    assert _post(client, token=None).status_code == 401


def test_bad_token_is_401(brain):
    client, _ = brain
    assert _post(client, token="wrong").status_code == 401


def test_health_needs_no_auth(brain):
    client, _ = brain
    assert client.get("/health").json()["ok"] is True


# ── baseline OpenAI shape ───────────────────────────────────────────────


def test_stateless_completion_shape(brain):
    client, _ = brain
    resp = _post(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "Hello from Narada"
    assert body["choices"][0]["finish_reason"] == "stop"
    # Stateless mode gets no tools by contract.
    assert FakeBackend.instances[-1].mcp_servers == {}


def test_streaming_chunks_and_done(brain):
    client, _ = brain
    resp = _post(client, stream=True, narada={"session_id": "s1"})
    assert resp.status_code == 200
    events = [line for line in resp.text.splitlines() if line.startswith("data: ")]
    assert events[-1] == "data: [DONE]"
    payloads = [json.loads(e[6:]) for e in events[:-1]]
    assert payloads[0]["choices"][0]["delta"] == {"role": "assistant"}
    text = "".join(p["choices"][0]["delta"].get("content", "")
                   for p in payloads)
    assert text == "Hello from Narada"
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"


def test_bad_body_is_400(brain):
    client, _ = brain
    resp = client.post("/v1/chat/completions", json={"messages": []},
                       headers={"Authorization": "Bearer tok-prana"})
    assert resp.status_code == 400


# ── sessions ────────────────────────────────────────────────────────────


def test_session_is_tier_namespaced(brain):
    client, config = brain
    _post(client, narada={"session_id": "shared"})
    _post(client, token="tok-app", narada={"session_id": "shared"})
    # Two distinct sessions on disk, namespaced by tier subdirectory.
    assert (config.sessions_root / "prana" / "shared").is_dir()
    assert (config.sessions_root / "app" / "shared").is_dir()


def test_session_persists_transcript(brain):
    client, config = brain
    _post(client, narada={"session_id": "s2"})
    lines = (config.sessions_root / "prana" / "s2" / "transcript.jsonl").read_text(
        encoding="utf-8").splitlines()
    roles = [json.loads(l)["role"] for l in lines]
    assert roles == ["user", "assistant"]


def test_invalid_session_id_rejected(brain):
    client, _ = brain
    resp = _post(client, narada={"session_id": "../evil"})
    assert resp.status_code == 400


# ── idempotency state machine ───────────────────────────────────────────


def test_retry_same_id_replays_not_reruns(brain):
    client, _ = brain
    kw = {"narada": {"session_id": "s3", "request_id": "r1"}}
    first = _post(client, **kw)
    n_backends = len(FakeBackend.instances)
    second = _post(client, **kw)
    assert second.status_code == 200
    assert second.json()["choices"][0]["message"]["content"] == \
        first.json()["choices"][0]["message"]["content"]
    assert len(FakeBackend.instances) == n_backends  # no new agent work


def test_same_id_different_prompt_is_422(brain):
    client, _ = brain
    _post(client, narada={"session_id": "s4", "request_id": "r1"})
    resp = _post(client,
                 messages=[{"role": "user", "content": "different"}],
                 narada={"session_id": "s4", "request_id": "r1"})
    assert resp.status_code == 422


def test_interrupted_recovery_requires_new_id(tmp_path):
    """A record left non-terminal (crash) loads as `interrupted`."""
    path = tmp_path / "turns.json"
    store = TurnStore(path)
    fp = fingerprint("s", "m", "p")
    store.accept("r9", fp)
    store.transition("r9", "running")
    # Simulate restart: a fresh store over the same file.
    recovered = TurnStore(path)
    rec = recovered.get("r9")
    assert rec.state == "interrupted"


def test_turn_store_window_evicts_oldest(tmp_path):
    store = TurnStore(tmp_path / "turns.json", window=3)
    for i in range(5):
        store.accept(f"r{i}", f"fp{i}")
        store.transition(f"r{i}", "completed", "x")
    assert store.get("r0") is None
    assert store.get("r4") is not None


# ── one active turn ─────────────────────────────────────────────────────


def test_concurrent_turn_same_session_is_409(brain):
    client, _ = brain

    # Slow down the backend so the first turn is still running.
    slow = lambda **kw: FakeBackend(**{**kw, "delay": 0.4})  # noqa: E731
    client.app.state.pool._factory = slow

    import threading
    results = {}

    def fire(tag):
        results[tag] = _post(client, narada={"session_id": "busy1"})

    t1 = threading.Thread(target=fire, args=("a",))
    t1.start()
    time.sleep(0.3)  # let turn a start
    fire("b")
    t1.join()
    codes = sorted([results["a"].status_code, results["b"].status_code])
    assert codes == [200, 409]


def test_same_request_id_race_never_runs_twice(brain):
    """Two concurrent requests with one request_id: one runs, the other
    gets 409/replay — never a second execution (diff-review P1)."""
    client, _ = brain
    slow = lambda **kw: FakeBackend(**{**kw, "delay": 0.4})  # noqa: E731
    client.app.state.pool._factory = slow

    import threading
    results = {}
    kw = {"narada": {"session_id": "race1", "request_id": "dup"}}

    def fire(tag):
        results[tag] = _post(client, **kw)

    t1 = threading.Thread(target=fire, args=("a",))
    t1.start()
    time.sleep(0.3)
    n_backends = len(FakeBackend.instances)
    fire("b")
    t1.join()
    codes = sorted([results["a"].status_code, results["b"].status_code])
    assert codes == [200, 409]
    assert len(FakeBackend.instances) == n_backends  # no second agent run


def test_backend_error_is_explicit_failure(brain):
    """A backend that errors mid-turn must yield an explicit 500 and a
    `failed` record — partial text never masquerades as completed."""
    client, config = brain

    class ErrorBackend(FakeBackend):
        async def run_turn(self, prompt):
            yield "partial "
            raise RuntimeError("agent turn ended in error (subtype=max_turns)")

    client.app.state.pool._factory = ErrorBackend
    resp = _post(client, narada={"session_id": "err1", "request_id": "e1"})
    assert resp.status_code == 500
    assert "error" in resp.json()
    turns = json.loads(
        (config.sessions_root / "prana" / "err1" / "turns.json").read_text(
            encoding="utf-8"))
    assert turns[0]["state"] == "failed"


def test_non_dict_body_and_narada_are_400(brain):
    client, _ = brain
    headers = {"Authorization": "Bearer tok-prana"}
    assert client.post("/v1/chat/completions", json=["not", "a", "dict"],
                       headers=headers).status_code == 400
    assert _post(client, narada="not-a-dict").status_code == 400


def test_stateless_deadline_is_explicit_error(tmp_path, monkeypatch):
    wake = tmp_path / "wake.md"
    wake.write_text("w", encoding="utf-8")
    config = BrainConfig(sessions_root=tmp_path / "s", wake_context=wake,
                         turn_deadline_s=0.2)
    monkeypatch.setattr("prana.brain.api.load_brain_tokens",
                        lambda: {"prana": "tok-prana", "app": "a", "voice": "v"})
    slow = lambda **kw: FakeBackend(**{**kw, "delay": 5.0})  # noqa: E731
    app = create_app(config, slow)
    with TestClient(app) as client:
        resp = _post(client)  # stateless: no session id
        assert resp.status_code == 500
        assert "wall-clock" in resp.json()["error"]["message"]


def test_auth_failures_rate_limited(brain):
    client, _ = brain
    for _ in range(10):
        assert _post(client, token="wrong").status_code == 401
    assert _post(client, token="wrong").status_code == 429
    # And the lockout is per-address for everyone, including good tokens
    # from that address, until the window passes.
    assert _post(client).status_code == 429


# ── cancel endpoint ─────────────────────────────────────────────────────


def test_cancel_no_session_404(brain):
    client, _ = brain
    resp = client.post("/v1/narada/sessions/nope/cancel",
                       headers={"Authorization": "Bearer tok-prana"})
    assert resp.status_code == 404


def test_cancel_idle_session_false(brain):
    client, _ = brain
    _post(client, narada={"session_id": "s5"})
    resp = client.post("/v1/narada/sessions/s5/cancel",
                       headers={"Authorization": "Bearer tok-prana"})
    assert resp.json() == {"cancelled": False}
