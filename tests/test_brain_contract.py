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
        self.model = model
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


# ── CORS (browser transport contract) ───────────────────────────────────

ALLOWED_ORIGIN = "capacitor://localhost"


def test_preflight_allowed_origin(brain):
    client, _ = brain
    resp = client.options(
        "/v1/chat/completions",
        headers={"Origin": ALLOWED_ORIGIN,
                 "Access-Control-Request-Method": "POST",
                 "Access-Control-Request-Headers": "content-type,authorization"})
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    allowed = resp.headers["access-control-allow-headers"].lower()
    assert "authorization" in allowed and "content-type" in allowed


def test_preflight_unknown_origin_refused(brain):
    client, _ = brain
    resp = client.options(
        "/v1/chat/completions",
        headers={"Origin": "https://evil.example",
                 "Access-Control-Request-Method": "POST"})
    assert resp.status_code == 400
    assert "access-control-allow-origin" not in resp.headers


def test_actual_post_carries_cors_header(brain):
    client, _ = brain
    resp = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer tok-prana",
                 "Origin": ALLOWED_ORIGIN})
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == ALLOWED_ORIGIN


def test_cors_origins_file_merges_without_env(tmp_path, monkeypatch):
    """The deploy-editable origins file is additive to defaults + env
    and picked up at process start — no host bounce needed."""
    monkeypatch.setenv("NARADA_BRAIN_CORS_ORIGINS", "https://from-env.example")
    f = tmp_path / "cors-origins.txt"
    f.write_text("# dev origins\nhttp://localhost:5173\n\nhttp://localhost:4174\n",
                 encoding="utf-8")
    config = BrainConfig(cors_origins_file=f)
    origins = config.cors_origins
    assert "capacitor://localhost" in origins
    assert "https://from-env.example" in origins
    assert "http://localhost:5173" in origins
    assert "http://localhost:4174" in origins
    assert "*" not in origins


def test_static_pwa_mounted_when_dir_exists(tmp_path, monkeypatch):
    wake = tmp_path / "wake.md"
    wake.write_text("w", encoding="utf-8")
    static = tmp_path / "pwa"
    static.mkdir()
    (static / "index.html").write_text("<html>narada</html>", encoding="utf-8")
    config = BrainConfig(sessions_root=tmp_path / "s", wake_context=wake,
                         static_dir=static)
    monkeypatch.setattr("prana.brain.api.load_brain_tokens",
                        lambda: {"prana": "t", "app": "a", "voice": "v"})
    app = create_app(config, FakeBackend)
    with TestClient(app) as client:
        resp = client.get("/app/")
        assert resp.status_code == 200
        assert "narada" in resp.text
        # The API surface is untouched by the mount.
        assert client.get("/health").json()["ok"] is True


# ── multimodal ──────────────────────────────────────────────────────────

DATA_URI = "data:image/jpeg;base64,aGVsbG8="


def _image_msg(text="what is this?", uri=DATA_URI):
    parts = [{"type": "image_url", "image_url": {"url": uri}}]
    if text:
        parts.append({"type": "text", "text": text})
    return [{"role": "user", "content": parts}]


def test_image_blocks_reach_backend(brain):
    client, _ = brain

    received = {}

    class RecordingBackend(FakeBackend):
        async def run_turn(self, prompt):
            received["prompt"] = prompt
            yield "a photo"

    client.app.state.pool._factory = RecordingBackend
    resp = _post(client, messages=_image_msg(),
                 narada={"session_id": "img1"})
    assert resp.status_code == 200
    blocks = received["prompt"]
    assert isinstance(blocks, list)
    assert blocks[0] == {"type": "image", "source": {
        "type": "base64", "media_type": "image/jpeg", "data": "aGVsbG8="}}
    assert blocks[1] == {"type": "text", "text": "what is this?"}


def test_image_only_message_accepted_in_session(brain):
    client, _ = brain
    resp = _post(client, messages=_image_msg(text=None),
                 narada={"session_id": "img2"})
    assert resp.status_code == 200


def test_image_without_session_is_clear_400(brain):
    client, _ = brain
    resp = _post(client, messages=_image_msg())
    assert resp.status_code == 400
    assert "session" in resp.json()["error"]["message"]


def test_remote_image_url_is_clear_400(brain):
    client, _ = brain
    resp = _post(client,
                 messages=_image_msg(uri="https://example.com/cat.jpg"),
                 narada={"session_id": "img3"})
    assert resp.status_code == 400
    assert "data:" in resp.json()["error"]["message"]


def test_same_request_id_different_image_is_422(brain):
    client, _ = brain
    kw = {"narada": {"session_id": "img4", "request_id": "r1"}}
    assert _post(client, messages=_image_msg(), **kw).status_code == 200
    other = _image_msg(uri="data:image/png;base64,b3RoZXI=")
    assert _post(client, messages=other, **kw).status_code == 422


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


# ── model pinning (plan §T2) ────────────────────────────────────────────


def test_model_pins_on_first_turn_and_persists(brain):
    client, config = brain
    r = _post(client, model="opus", narada={"session_id": "s-pin"})
    assert r.status_code == 200
    assert FakeBackend.instances[-1].model == "opus"
    assert r.json()["model"] == "opus"
    assert (config.sessions_root / "prana" / "s-pin" / "model").read_text() == "opus"
    # Omitting the model on a later turn resolves to the pin (the
    # helper's default body names sonnet, so drop it explicitly).
    r2 = _post(client, model=None, narada={"session_id": "s-pin"})
    assert r2.status_code == 200
    assert r2.json()["model"] == "opus"


def test_different_model_repins_on_the_same_transcript(brain):
    """Suti 2026-09-08: the model is changeable mid-chat. The warm client
    is replaced by one on the new model that RESUMES the same native
    transcript; the pin file follows; a later turn without a model
    lands on the new pin."""
    client, config = brain
    _post(client, model="sonnet", narada={"session_id": "s-conf"})
    first = FakeBackend.instances[-1]
    r = _post(client, model="haiku", narada={"session_id": "s-conf"})
    assert r.status_code == 200
    assert r.json()["model"] == "haiku"
    second = FakeBackend.instances[-1]
    assert second is not first and second.model == "haiku"
    assert first.closed
    assert second.resume == first.native_session_id
    assert (config.sessions_root / "prana" / "s-conf" / "model").read_text() == "haiku"
    r3 = _post(client, model=None, narada={"session_id": "s-conf"})
    assert r3.json()["model"] == "haiku"
    assert FakeBackend.instances[-1] is second  # no churn when the pin matches


def test_model_switch_waits_for_the_running_turn(brain):
    client, _ = brain
    _post(client, model="sonnet", narada={"session_id": "s-busy-model"})
    import asyncio

    from prana.brain.session import SessionBusy

    pool = client.app.state.pool
    session = pool.peek("s-busy-model", "prana")
    session.reserve()
    try:
        with pytest.raises(SessionBusy):
            asyncio.run(pool.switch_model("s-busy-model", "prana", "opus"))
    finally:
        session.release()
    assert session.model == "sonnet"


def test_replay_and_rejected_requests_never_move_the_pin(brain):
    """Review: a retry (or a 422'd reuse) that names another model must
    replay/refuse exactly as before — and leave the pin where the last
    NEW turn put it."""
    client, config = brain
    pin = config.sessions_root / "prana" / "s-replay" / "model"
    r1 = _post(client, model="sonnet", narada={"session_id": "s-replay", "request_id": "r1"})
    assert r1.status_code == 200
    r2 = _post(client, model="haiku", narada={"session_id": "s-replay", "request_id": "r2"})
    assert r2.status_code == 200 and pin.read_text() == "haiku"
    n = len(FakeBackend.instances)
    # Retry r1 with its original model: a replay, no switch, pin stays.
    again = _post(client, model="sonnet", narada={"session_id": "s-replay", "request_id": "r1"})
    assert again.status_code == 200
    assert again.json()["choices"][0]["message"]["content"] == \
        r1.json()["choices"][0]["message"]["content"]
    assert pin.read_text() == "haiku" and len(FakeBackend.instances) == n
    # Reuse r1 with a third model: 422, and still no switch.
    bad = _post(client, model="opus", narada={"session_id": "s-replay", "request_id": "r1"})
    assert bad.status_code == 422
    assert pin.read_text() == "haiku" and len(FakeBackend.instances) == n
    # A turn omitting the model lands on the pin.
    r3 = _post(client, model=None, narada={"session_id": "s-replay"})
    assert r3.json()["model"] == "haiku"


def test_switch_guard_runs_under_the_lock_and_refuses(tmp_path):
    """Recheck: a request that passed its record check, then waited for
    the pool lock while a queued request completed the same request_id,
    must not move the pin — the guard sees the completed record."""
    import asyncio

    from prana.brain.session import SessionPool, SwitchRefused

    wake = tmp_path / "wake-context.md"
    wake.write_text("ctx", encoding="utf-8")
    config = BrainConfig(sessions_root=tmp_path / "sessions", wake_context=wake)

    async def scenario():
        pool = SessionPool(config, FakeBackend)
        first = await pool.get_or_create("s-guard", "prana", "sonnet")
        # Someone else completes r1 while our switch waits for the lock.
        first.turns.accept("r1", "fp-r1")
        first.turns.transition("r1", "completed", "done")
        with pytest.raises(SwitchRefused):
            await pool.switch_model("s-guard", "prana", "haiku",
                                    guard=lambda warm: warm.turns.get("r1") is None)
        assert pool.peek("s-guard", "prana") is first and first.model == "sonnet"
        assert (config.sessions_root / "prana" / "s-guard" / "model").read_text() == "sonnet"
        # A truthful guard lets the switch through.
        switched = await pool.switch_model("s-guard", "prana", "haiku",
                                           guard=lambda warm: True)
        assert switched.model == "haiku"

    asyncio.run(scenario())


def test_reaper_does_not_double_close_a_session_mid_switch(tmp_path):
    """Review: while switch_model awaits the old backend's close, the
    reaper must not find that session, close it again, or make the
    switch fail with KeyError."""
    import asyncio

    from prana.brain.session import SessionPool

    wake = tmp_path / "wake-context.md"
    wake.write_text("ctx", encoding="utf-8")
    config = BrainConfig(sessions_root=tmp_path / "sessions", wake_context=wake,
                         idle_ttl_s=0.0)

    class SlowClose(FakeBackend):
        gate: "asyncio.Event"
        closes = 0

        async def close(self):
            SlowClose.closes += 1
            await SlowClose.gate.wait()
            await super().close()

    async def scenario():
        SlowClose.gate = asyncio.Event()
        pool = SessionPool(config, SlowClose)
        first = await pool.get_or_create("s-race", "prana", "sonnet")
        first.last_used = 0.0  # idle since forever
        switching = asyncio.create_task(pool.switch_model("s-race", "prana", "haiku"))
        await asyncio.sleep(0)  # let the switch reach the awaited close
        assert SlowClose.closes == 1
        await pool.reap_once()  # must not see the half-switched session
        assert SlowClose.closes == 1
        SlowClose.gate.set()
        switched = await switching
        assert switched.model == "haiku" and pool.peek("s-race", "prana") is switched

    asyncio.run(scenario())


def test_unknown_model_is_400(brain):
    client, _ = brain
    r = _post(client, model="gpt-9", narada={"session_id": "s-bad"})
    assert r.status_code == 400


def test_pin_survives_restart(brain):
    client, config = brain
    _post(client, model="haiku", narada={"session_id": "s-again"})
    # A brand-new server over the same sessions_root — the restart case
    # (and the post-reap recreation case: no warm session, only files).
    fresh = create_app(config, FakeBackend)
    with TestClient(fresh) as client2:
        r = _post(client2, model=None, narada={"session_id": "s-again"})
        assert r.status_code == 200
        assert r.json()["model"] == "haiku"
        assert FakeBackend.instances[-1].model == "haiku"
        # And a different model against the recreated session re-pins it.
        r = _post(client2, model="opus", narada={"session_id": "s-again"})
        assert r.status_code == 200
        assert r.json()["model"] == "opus"
        assert (config.sessions_root / "prana" / "s-again" / "model").read_text() == "opus"


def test_models_endpoint_lists_the_allowlist(brain):
    client, _ = brain
    r = client.get("/v1/models", headers={"Authorization": "Bearer tok-prana"})
    ids = [m["id"] for m in r.json()["data"]]
    assert ids == ["sonnet", "opus", "haiku"]
