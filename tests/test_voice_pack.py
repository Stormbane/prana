"""B3 — two-pack context: disjoint roots, separate builders, sentinel."""

from __future__ import annotations

from pathlib import Path

import pytest

from prana.voice import pack


@pytest.fixture()
def roots(tmp_path, monkeypatch):
    shareable = tmp_path / "voice-pack" / "shareable"
    personal = tmp_path / "voice-pack" / "personal"
    shareable.mkdir(parents=True)
    personal.mkdir(parents=True)
    monkeypatch.setattr(pack, "SHAREABLE_DIR", shareable)
    monkeypatch.setattr(pack, "PERSONAL_DIR", personal)
    monkeypatch.setattr(pack, "OPEN_THREADS", tmp_path / "none.md")
    (shareable / "a.md").write_text("I am Narada.", encoding="utf-8")
    (personal / "suti.md").write_text(
        "CANARY-9f2e the person is Suti", encoding="utf-8")
    return shareable, personal


def test_sentinel_personal_never_in_shareable_output(roots):
    """The M2 round-2 sentinel: the canary in personal/ must NEVER
    appear in the shareable builder's output."""
    out = pack.build_shareable()
    assert "I am Narada." in out
    assert "CANARY-9f2e" not in out


def test_personal_includes_both(roots):
    out = pack.build_personal()
    assert "I am Narada." in out
    assert "CANARY-9f2e" in out


def test_tier_dispatch(roots):
    assert "CANARY-9f2e" not in pack.build_for_tier("shareable")
    assert "CANARY-9f2e" in pack.build_for_tier("personal")
    # Unknown tiers degrade to shareable, never up.
    assert "CANARY-9f2e" not in pack.build_for_tier("weird")


def test_no_recursion_no_smuggling(roots):
    shareable, _ = roots
    nested = shareable / "nested"
    nested.mkdir()
    (nested / "smuggled.md").write_text("SMUGGLED", encoding="utf-8")
    assert "SMUGGLED" not in pack.build_shareable()


def test_caps_enforced(roots):
    shareable, _ = roots
    (shareable / "big.md").write_text("x" * 10_000, encoding="utf-8")
    assert len(pack.build_shareable()) <= pack.SHAREABLE_CAP + 50


def test_missing_dirs_mean_empty_not_error(tmp_path, monkeypatch):
    monkeypatch.setattr(pack, "SHAREABLE_DIR", tmp_path / "no")
    monkeypatch.setattr(pack, "PERSONAL_DIR", tmp_path / "nope")
    monkeypatch.setattr(pack, "OPEN_THREADS", tmp_path / "none.md")
    assert pack.build_shareable() == ""
    assert pack.build_personal() == ""


def test_thread_headlines_only(tmp_path, roots, monkeypatch):
    threads = tmp_path / "open-threads.md"
    threads.write_text(
        "# The body question\nsecret body text\n"
        "## LoRA drift\nprivate details here\n", encoding="utf-8")
    monkeypatch.setattr(pack, "OPEN_THREADS", threads)
    out = pack.build_personal()
    assert "The body question" in out and "LoRA drift" in out
    assert "secret body text" not in out
    assert "private details" not in out
