"""The admission-gap device-drop race (M2a review round-2).

Models the exact ordering the reviewer flagged: a fresh per-session
drop event is installed, then presence is checked with no await between.
A drop that fires *after* install is caught by the event; a drop *before*
install is caught by the presence check. Neither can be silently erased.
"""

from __future__ import annotations

import asyncio


class _FakeRoom:
    def __init__(self, present: bool):
        self.remote_participants = {"narada-box3": object()} if present else {}


def _make_session_guard():
    """Mirror the worker's drop/presence handshake in isolation."""
    drop = {"ev": asyncio.Event()}

    def on_disconnect(room):
        # handler always targets the CURRENT event
        drop["ev"].set()

    def admit(room):
        drop["ev"] = asyncio.Event()          # fresh, before the check
        present = "narada-box3" in room.remote_participants  # no await
        return present, drop

    return admit, on_disconnect


def test_drop_after_install_is_caught():
    admit, on_disconnect = _make_session_guard()
    room = _FakeRoom(present=True)
    present, drop = admit(room)
    assert present is True
    # device drops AFTER admission passed
    on_disconnect(room)
    assert drop["ev"].is_set()  # session will terminate on device-dropped


def test_drop_before_install_is_caught_by_presence():
    admit, _ = _make_session_guard()
    room = _FakeRoom(present=False)  # already gone
    present, drop = admit(room)
    assert present is False  # refused at admission; no billed session


def test_new_event_not_falsely_preset():
    admit, on_disconnect = _make_session_guard()
    room = _FakeRoom(present=True)
    # a drop from a PRIOR session must not carry into the next
    on_disconnect(room)          # prior session's event set
    present, drop = admit(room)  # new session installs a fresh event
    assert present is True
    assert not drop["ev"].is_set()
