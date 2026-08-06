"""Foreign-session discovery — scan Claude Code's own transcript files.

Every Claude Code session (whoever started it, in whatever terminal)
streams its transcript to ``~/.claude/projects/<encoded-cwd>/<uuid>.jsonl``.
Scanning mtimes + parsing the tail is the ecosystem-standard way to see
them (claude-code-log, Codecast, CCC all do this).

The format is INTERNAL to Claude Code and drifts between releases. All
parsing stays in this module; tests pin the current expectations so a
drift breaks loudly here, not quietly everywhere.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# A session whose transcript changed this recently is considered active.
ACTIVE_WINDOW_S = 120


@dataclass
class ForeignSession:
    session_id: str            # transcript filename uuid
    project_dir: str           # encoded cwd (directory name under projects/)
    transcript: Path
    mtime: datetime
    active: bool               # mtime within ACTIVE_WINDOW_S
    last_role: Optional[str]   # 'user' | 'assistant' | None if unparseable
    summary: str               # best-effort last text, truncated


def _decode_project_dir(name: str) -> str:
    """``C--Projects-prana`` → best-effort readable path.

    The encoding is lossy (both ``\\`` and ``.`` become ``-``); this is
    for display only — never use it to reconstruct a real path.
    """
    return name.replace("--", ":\\").replace("-", "\\")


def _tail_lines(path: Path, max_bytes: int = 64 * 1024) -> list[str]:
    """Read the last chunk of a file without loading the whole thing."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError as exc:
        logger.debug("tail failed for %s: %s", path, exc)
        return []
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    # first line may be a partial record if we seeked mid-line
    return lines[1:] if size > max_bytes else lines


def _last_message(lines: list[str]) -> tuple[Optional[str], str]:
    """Walk the tail backwards for the last user/assistant text."""
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("type") not in ("user", "assistant"):
            continue
        content = obj.get("message", {}).get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        else:
            continue
        text = text.strip()
        if not text or text.startswith("<system-reminder>"):
            continue
        return obj["type"], text[:200]
    return None, ""


def scan(
    projects_dir: Path = CLAUDE_PROJECTS_DIR,
    *,
    max_age: timedelta = timedelta(days=2),
) -> list[ForeignSession]:
    """Enumerate recent Claude Code sessions, newest first."""
    if not projects_dir.is_dir():
        return []
    now = datetime.now(timezone.utc)
    cutoff = now - max_age
    found: list[ForeignSession] = []
    for project in projects_dir.iterdir():
        if not project.is_dir():
            continue
        try:
            entries = list(project.glob("*.jsonl"))
        except OSError:
            continue
        for transcript in entries:
            try:
                mtime = datetime.fromtimestamp(
                    transcript.stat().st_mtime, tz=timezone.utc
                )
            except OSError:
                continue
            if mtime < cutoff:
                continue
            lines = _tail_lines(transcript)
            role, summary = _last_message(lines)
            found.append(
                ForeignSession(
                    session_id=transcript.stem,
                    project_dir=project.name,
                    transcript=transcript,
                    mtime=mtime,
                    active=(now - mtime).total_seconds() <= ACTIVE_WINDOW_S,
                    last_role=role,
                    summary=summary,
                )
            )
    found.sort(key=lambda s: s.mtime, reverse=True)
    return found
