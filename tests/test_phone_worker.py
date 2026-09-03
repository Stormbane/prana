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
