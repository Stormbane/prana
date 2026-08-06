"""Foreign-session watcher against fixture transcripts.

Pins the current jsonl expectations so a Claude Code format drift breaks
loudly here instead of quietly everywhere.
"""

from __future__ import annotations

import json
import os
import time

from prana.sessions.watcher import scan


def _write_transcript(project_dir, session_id, records, mtime_ago_s=0):
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{session_id}.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) for r in records), encoding="utf-8"
    )
    if mtime_ago_s:
        old = time.time() - mtime_ago_s
        os.utime(path, (old, old))
    return path


def _user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant(text):
    return {
        "type": "assistant",
        "message": {"role": "assistant",
                    "content": [{"type": "text", "text": text}]},
    }


def test_scan_finds_sessions_and_last_message(tmp_path):
    proj = tmp_path / "C--Projects-demo"
    _write_transcript(proj, "aaaa-bbbb", [
        {"type": "summary", "summary": "ignored"},
        _user("fix the login bug"),
        _assistant("Done — the guard clause was inverted."),
    ])
    found = scan(tmp_path)
    assert len(found) == 1
    s = found[0]
    assert s.session_id == "aaaa-bbbb"
    assert s.active is True
    assert s.last_role == "assistant"
    assert "guard clause" in s.summary


def test_scan_skips_system_reminders_and_old_files(tmp_path):
    proj = tmp_path / "C--Projects-demo"
    _write_transcript(proj, "recent", [
        _user("<system-reminder>noise</system-reminder>"),
        _user("real question"),
    ])
    _write_transcript(proj, "ancient", [_user("old")], mtime_ago_s=10 * 86400)
    found = scan(tmp_path)
    assert [s.session_id for s in found] == ["recent"]
    assert found[0].summary == "real question"


def test_scan_inactive_when_stale(tmp_path):
    proj = tmp_path / "C--Projects-demo"
    _write_transcript(proj, "stale", [_user("hi")], mtime_ago_s=3600)
    found = scan(tmp_path)
    assert found[0].active is False


def test_scan_handles_garbage_lines(tmp_path):
    proj = tmp_path / "C--Projects-demo"
    path = proj / "junk.jsonl"
    proj.mkdir(parents=True)
    path.write_text("not json\n{\"type\": \"weird\"}\n", encoding="utf-8")
    found = scan(tmp_path)
    assert len(found) == 1
    assert found[0].last_role is None


def test_scan_missing_dir_is_empty(tmp_path):
    assert scan(tmp_path / "nope") == []
