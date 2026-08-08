"""Transcript logging: writes both sides, fail-open on IO errors."""

from __future__ import annotations

from prana.voice.transcripts import TranscriptLogger, text_of


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
    assert "started:" in body and "ended:" in body


def test_empty_utterances_skipped(tmp_path):
    tl = TranscriptLogger("room-2", root=tmp_path)
    tl.log("user", "   ")
    tl.log("assistant", "")
    lines = [l for l in tl.path.read_text(encoding="utf-8").splitlines()
             if l.startswith("- ")]
    assert lines == []


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
    # must not raise — the conversation continues even if logging fails
    tl.log("user", "this should not crash")
    tl.close()
