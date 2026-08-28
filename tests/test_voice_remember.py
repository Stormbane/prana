"""B1 — quarantined voice memory (resilience-and-reach).

The contract: everything the voice writes lands in inbox/voice and only
there; inbox is never recallable; summaries need the personal tier;
quotas survive restarts (the filesystem is the ledger).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from prana.voice import memory, remember


def test_note_lands_in_inbox_with_metadata(tmp_path: Path):
    p = remember.write_note("the mango tree needs water", tier="personal",
                            session_id="AJ_123", inbox=tmp_path)
    assert p.parent == tmp_path
    body = p.read_text(encoding="utf-8")
    assert "origin: voice" in body
    assert "tier: personal" in body
    assert "status: pending-review" in body
    assert "mango tree" in body


def test_payload_cannot_choose_the_path(tmp_path: Path):
    evil = "../../people/suti/suti.md\nnote: injected"
    p = remember.write_note(evil, tier="shareable", session_id="x",
                            inbox=tmp_path)
    # The filename is ours; the payload is content, nowhere else.
    assert p.parent == tmp_path
    assert ".." not in p.name and "/" not in p.name


def test_metadata_fields_are_sanitized(tmp_path: Path):
    import re
    p = remember.write_note("hi", tier="personal\n---\nevil: y",
                            session_id="a/b\\c\n", inbox=tmp_path)
    body = p.read_text(encoding="utf-8")
    # The real property: crafted values cannot break out of their
    # frontmatter line — no injected keys, no early block terminator.
    assert re.search(r"^evil:", body, re.M) is None
    # Exactly the open/close fence LINES (a "---" inside a sanitized
    # value stays mid-line and is not a fence).
    assert len(re.findall(r"^---\s*$", body, re.M)) == 2
    tier_line = re.search(r"^tier: (.+)$", body, re.M).group(1)
    assert re.fullmatch(r"[a-z-]+", tier_line)


def test_summary_refused_for_shareable_tier(tmp_path: Path):
    assert remember.write_session_summary(
        "guest session summary", tier="shareable", session_id="s",
        inbox=tmp_path) is None
    assert list(tmp_path.glob("*.md")) == []


def test_summary_written_for_personal_tier(tmp_path: Path):
    p = remember.write_session_summary(
        "we talked about the garden", tier="personal", session_id="s",
        inbox=tmp_path)
    assert p is not None
    assert "kind: session-summary" in p.read_text(encoding="utf-8")


def test_quota_counts_files_on_disk(tmp_path: Path):
    for i in range(remember.MAX_NOTES_PER_DAY):
        remember.write_note(f"note {i}", tier="shareable", session_id="s",
                            inbox=tmp_path)
    with pytest.raises(remember.QuotaExceeded):
        remember.write_note("one too many", tier="shareable",
                            session_id="s", inbox=tmp_path)
    # A "restarted worker" (fresh call, same disk) still sees the quota.
    with pytest.raises(remember.QuotaExceeded):
        remember.write_note("still too many", tier="shareable",
                            session_id="s", inbox=tmp_path)


def test_secrets_redacted(tmp_path: Path):
    p = remember.write_note("the key is sk-abcdefghijklmnop123456",
                            tier="personal", session_id="s", inbox=tmp_path)
    body = p.read_text(encoding="utf-8")
    assert "sk-abcdefghijklmnop123456" not in body
    assert "[REDACTED]" in body


def test_sentinel_inbox_is_never_recallable():
    """The quarantine's load-bearing wall: `inbox` must be on the recall
    hard denylist. If someone ever removes it, voice writes become a
    prompt-injection persistence channel and this test is the tripwire."""
    assert "inbox" in memory.NEVER_RECALLABLE
    roots = memory._resolve_safe_roots(memory.NARADA_ROOT,
                                       memory.VOICE_RECALLABLE_BRANCHES)
    for _, path in roots:
        assert "inbox" not in path.parts
