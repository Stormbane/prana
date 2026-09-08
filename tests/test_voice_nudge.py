"""The shake nudge: one at a time, never queued behind speech."""

from __future__ import annotations

from prana.voice.phone_worker import NUDGE_COOLDOWN_S, nudge_allowed


class Handle:
    def __init__(self, finished: bool):
        self._finished = finished

    def done(self) -> bool:
        return self._finished


def test_cooldown_and_pending_reply_block_a_second_nudge():
    assert nudge_allowed(10.0, 0.0, None, None) is True
    assert nudge_allowed(10.0 + NUDGE_COOLDOWN_S - 0.1, 10.0, None, None) is False
    assert nudge_allowed(10.0 + NUDGE_COOLDOWN_S + 0.1, 10.0, None, None) is True
    # Our previous nudge is still generating/playing: no second one.
    assert nudge_allowed(20.0, 10.0, Handle(finished=False), None) is False
    assert nudge_allowed(20.0, 10.0, Handle(finished=True), None) is True


def test_active_speech_blocks_a_nudge():
    assert nudge_allowed(20.0, 0.0, None, Handle(finished=False)) is False
    assert nudge_allowed(20.0, 0.0, None, Handle(finished=True)) is True


def test_duck_typing_never_raises():
    class Odd:
        def done(self):
            raise RuntimeError("no")

    assert nudge_allowed(20.0, 0.0, Odd(), object()) is True
