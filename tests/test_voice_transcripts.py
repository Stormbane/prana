"""Transcript logging + privacy controls (cross-review #3)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import prana.voice.transcripts as tmod
from prana.voice.transcripts import (
    TranscriptLogger,
    prune_old,
    redact,
    text_of,
)


class FakeItem:
    def __init__(self, role, text=None, content=None):
        self.role = role
        if text is not None:
            self.text_content = text
        if content is not None:
            self.content = content


def test_logs_both_sides(tmp_path):
    tl = TranscriptLogger("room-1", root=tmp_path)
    tl.log("user", "what sessions do I have open")
    tl.log("assistant", "you have two active sessions")
    tl.close("done")
    body = tl.path.read_text(encoding="utf-8")
    assert "**user:** what sessions do I have open" in body
    assert "**assistant:** you have two active sessions" in body
    assert "RECORDING ACTIVE" in body and "RECORDING STOPPED" in body


def test_secrets_are_redacted(tmp_path):
    tl = TranscriptLogger("room-secret", root=tmp_path)
    tl.log("user", "my key is sk-proj-ABCDEFGHIJKLMNOP1234567890 keep it safe")
    tl.close()
    body = tl.path.read_text(encoding="utf-8")
    assert "sk-proj-ABCDEFGHIJKLMNOP" not in body
    assert "[REDACTED]" in body


def test_redact_function():
    assert "[REDACTED]" in redact("token sk-abcdefghijklmnop1234 here")
    assert redact("nothing secret here") == "nothing secret here"


def test_recording_marker_lifecycle(tmp_path, monkeypatch):
    marker = tmp_path / "marker"
    monkeypatch.setattr(tmod, "RECORDING_MARKER", marker)
    tl = TranscriptLogger("room-m", root=tmp_path)
    assert marker.exists()  # recording indicator on during session
    tl.close()
    assert not marker.exists()  # cleared when session ends


def test_recording_marker_refcounts_concurrent_sessions(tmp_path, monkeypatch):
    # Two workers (box + phone), or a soak's sim beside a real session:
    # one ending must not switch the shared indicator off while the
    # other still records (Codex review, rung 1b).
    marker = tmp_path / "marker"
    monkeypatch.setattr(tmod, "RECORDING_MARKER", marker)
    a = TranscriptLogger("narada-body", root=tmp_path)
    b = TranscriptLogger("akhada-phone-sim-1", root=tmp_path)
    assert marker.exists()
    b.close()
    assert marker.exists()  # the box session is still recording
    assert "narada-body" in marker.read_text()
    a.close()
    assert not marker.exists()


def test_recording_marker_keys_do_not_alias(tmp_path, monkeypatch):
    # _safe() truncates to 40 chars — two long room names must still
    # hold two references (hash suffix), or one close drops the other.
    marker = tmp_path / "marker"
    monkeypatch.setattr(tmod, "RECORDING_MARKER", marker)
    long_a = "akhada-phone-" + "x" * 60 + "a"
    long_b = "akhada-phone-" + "x" * 60 + "b"
    a = TranscriptLogger(long_a, root=tmp_path)
    b = TranscriptLogger(long_b, root=tmp_path)
    b.close()
    assert marker.exists()  # a's reference survives b's close
    a.close()
    assert not marker.exists()


def test_recording_marker_reaps_crashed_sessions(tmp_path, monkeypatch):
    # A killed worker never calls close(); its sidecar entry must not
    # pin the indicator on forever.
    import os
    marker = tmp_path / "marker"
    monkeypatch.setattr(tmod, "RECORDING_MARKER", marker)
    dead = TranscriptLogger("crashed-room", root=tmp_path)  # no close()
    sidecar = marker.parent / (marker.name + ".d")
    (entry,) = sidecar.iterdir()
    old = 1.0  # epoch 1970 — very stale
    os.utime(entry, (old, old))
    live = TranscriptLogger("live-room", root=tmp_path)
    assert not entry.exists()  # reaped on the next marker update
    live.close()
    assert not marker.exists()  # only the reaped ghost remained
    del dead


def test_retention_prunes_old(tmp_path):
    d = tmp_path / "2020_01"
    d.mkdir(parents=True)
    old = d / "old.md"
    old.write_text("ancient", encoding="utf-8")
    old_time = time.time() - 40 * 86400
    import os
    os.utime(old, (old_time, old_time))
    recent = d / "recent.md"
    recent.write_text("fresh", encoding="utf-8")
    removed = prune_old(tmp_path, days=30)
    assert removed == 1
    assert not old.exists() and recent.exists()


def test_text_of_variants():
    assert text_of(FakeItem("user", text="hi")) == "hi"
    assert text_of(FakeItem("user", content="plain")) == "plain"
    assert text_of(FakeItem("user", content=["a", "b"])) == "a b"
    assert text_of(FakeItem("user")) == ""


def test_write_failure_is_fail_open(tmp_path, monkeypatch):
    tl = TranscriptLogger("room-3", root=tmp_path)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("builtins.open", boom)
    tl.log("user", "this should not crash")
    tl.close()
