"""Phone worker invariants that don't need a LiveKit server."""

from __future__ import annotations

from livekit import rtc

from prana.voice import phone_worker, worker as box


class _P:
    def __init__(self, kind):
        self.kind = kind


class _FakeRoom:
    def __init__(self, participants):
        self.remote_participants = {
            f"p{i}": p for i, p in enumerate(participants)}


def test_humans_filters_agents_out():
    agent = _P(rtc.ParticipantKind.PARTICIPANT_KIND_AGENT)
    human = _P(rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD)
    assert phone_worker._humans(_FakeRoom([])) == []
    assert phone_worker._humans(_FakeRoom([agent])) == []
    assert phone_worker._humans(_FakeRoom([agent, human])) == [human]


def test_phone_ports_do_not_collide_with_box_defaults():
    # Both workers run on one machine; a port collision would make one
    # health probe vouch for the other worker's life.
    assert {phone_worker.AGENTS_PORT, phone_worker.HEALTH_PORT} \
        .isdisjoint({8792, 8793})


def test_agent_name_default_is_explicit_dispatch_only():
    # Empty agent_name would mean automatic dispatch into every room —
    # the box worker's exclusive right.
    assert phone_worker.DEFAULT_AGENT_NAME


def test_box_declines_exactly_the_phone_prefix():
    # The decline predicate and the phone worker's world must agree;
    # drift re-opens the ghost-agent hole (field 2026-09-04).
    assert box.is_phone_room("akhada-phone-1788449012902-eb9c43")
    assert box.is_phone_room("akhada-phone-sim-1")
    assert not box.is_phone_room("narada-body")
    assert not box.is_phone_room("narada-body-sim")
    assert not box.is_phone_room("")
    assert not box.is_phone_room(None)
    assert phone_worker.DEFAULT_AGENT_NAME.startswith("akhada-phone")


def test_prefix_agrees_with_akhada_dispatch_endpoint():
    try:
        from akhada.dashboard.voice import ROOM_PREFIX
    except ImportError:
        import pytest
        pytest.skip("akhada not installed here")
    assert ROOM_PREFIX == box.PHONE_ROOM_PREFIX



def test_context_pack_reads_the_named_session_only(tmp_path, monkeypatch):
    import json
    from pathlib import Path

    from prana.voice.phone_worker import context_pack

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    d = tmp_path / ".narada" / "brain" / "sessions" / "app" / "phone-1"
    d.mkdir(parents=True)
    lines = [
        {"role": "user", "content": "log my lunch: chicken and rice"},
        {"role": "assistant", "content": "Logged lunch — 620 kcal."},
        {"role": "system", "content": "turn failed: x"},
        {"role": "user", "content": "x" * 500},
    ]
    (d / "transcript.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    pack = context_pack(json.dumps({"session_id": "phone-1", "tier": "app"}))
    assert "Suti: log my lunch" in pack and "Narada: Logged lunch" in pack
    assert "turn failed" not in pack           # system lines never leak
    assert "x" * 321 not in pack and "…" in pack  # long turns truncate
    # Foreign tier, missing session, malformed metadata: silent empty.
    assert context_pack(json.dumps({"session_id": "phone-1", "tier": "prana"})) == ""
    assert context_pack(json.dumps({"session_id": "nope", "tier": "app"})) == ""
    assert context_pack("not json") == "" and context_pack(None) == ""
    assert context_pack(json.dumps({"session_id": "../phone-1", "tier": "app"})) == ""


# ── setup failures still report (Codex recheck, 2026-09-14) ───────────

class _Human:
    identity = "suti-phone"
    kind = None  # not an agent


class _LocalParticipant:
    def __init__(self):
        self.published = []

    async def publish_data(self, data, topic="", reliable=True, **_kw):
        import json

        if isinstance(data, (bytes, bytearray)):
            data = data.decode("utf-8")
        self.published.append((topic, json.loads(data)))


class _Room:
    name = "phone-test-room"

    def __init__(self):
        self.remote_participants = {"suti-phone": _Human()}
        self.local_participant = _LocalParticipant()

    def on(self, *_a, **_kw):
        return None


class _Job:
    id = "job-test"
    metadata = None


class _Ctx:
    def __init__(self):
        self.room = _Room()
        self.job = _Job()
        self.shutdown_reason = None

    async def connect(self):
        return None

    def shutdown(self, reason=""):
        self.shutdown_reason = reason


class _Budget:
    def reserve(self):
        return object()

    def settle(self, *_a):
        return None


def test_a_setup_failure_before_start_still_reports_a_fault(tmp_path, monkeypatch):
    # The session never starts: a tool-builder crash mid-setup. The app
    # must still get a fault message and the feed an entry — the finally
    # reports whatever the outer except noted.
    import asyncio
    from pathlib import Path

    import pytest

    from prana.voice import phone_worker as pw

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(pw, "VoiceBudget", _Budget)

    def boom(**_kw):
        raise ValueError("could not convert string to float: 'x'")

    monkeypatch.setattr(pw, "build_voice_tools", boom)
    notified = []
    monkeypatch.setattr(pw, "notify_brain", lambda fault, surface: notified.append((fault.code, surface)))

    ctx = _Ctx()
    with pytest.raises(ValueError):
        asyncio.run(pw.entrypoint(ctx))

    faults = [m for t, m in ctx.room.local_participant.published
              if t == "narada.activity" and m.get("type") == "fault"]
    assert faults and faults[-1]["code"] == "unknown" and faults[-1]["sticky"] is True
    assert notified == [("unknown", "phone")]
