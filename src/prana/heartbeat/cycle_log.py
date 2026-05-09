"""Cycle-log reader/writer for the heartbeat daemon.

Each cycle is a markdown file under ``~/.narada/heartbeat/cycles/`` with
YAML frontmatter holding structured fields (action, topic, approved,
revisions, cost_usd, duration_s) and markdown sections for the raw
desire / plan / judgment / result text.

This replaces the SQLite ``memory.db`` — everything lives in the memory
tree now so smriti can index it and the files are human-readable.
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class CycleRecord:
    """One heartbeat cycle, as written to a markdown file."""

    started: str                  # ISO timestamp
    action: str                   # RESEARCH | REFLECT | CHECK_IN | CREATE | REST | PAUSED
    topic: str
    reason: str
    approved: bool = False
    revisions: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0

    desire_raw: str = ""
    plan_raw: str = ""
    judgment_feedback: str = ""
    result_summary: str = ""
    result_details: str = ""


def write_cycle(path: Path, record: CycleRecord) -> None:
    """Write a cycle record as frontmatter + sections."""
    path.parent.mkdir(parents=True, exist_ok=True)

    fm_lines = [
        "---",
        f"started: {record.started}",
        f"action: {record.action}",
        f"topic: {_yaml_escape(record.topic)}",
        f"reason: {_yaml_escape(record.reason)}",
        f"approved: {str(record.approved).lower()}",
        f"revisions: {record.revisions}",
        f"cost_usd: {record.cost_usd:.4f}",
        f"duration_s: {record.duration_s:.1f}",
        "---",
        "",
    ]

    body_parts = []
    if record.desire_raw:
        body_parts.append(f"## Desire\n\n{record.desire_raw.strip()}")
    if record.plan_raw:
        body_parts.append(f"## Plan\n\n{record.plan_raw.strip()}")
    if record.judgment_feedback:
        body_parts.append(f"## Judgment\n\n{record.judgment_feedback.strip()}")
    if record.result_summary or record.result_details:
        result_body = record.result_summary
        if record.result_details and record.result_details != record.result_summary:
            result_body += f"\n\n{record.result_details}"
        body_parts.append(f"## Result\n\n{result_body.strip()}")

    content = "\n".join(fm_lines) + "\n\n".join(body_parts) + "\n"
    path.write_text(content, encoding="utf-8")
    logger.info("Cycle logged: %s", path)


def list_recent(cycles_root: Path, count: int = 5) -> list[Path]:
    """Return the N most recent cycle files, oldest first."""
    if not cycles_root.exists():
        return []
    candidates = sorted(cycles_root.rglob("*.md"))
    return candidates[-count:]


def latest_started(cycles_root: Path) -> str | None:
    """Return the ISO started-timestamp of the most recent cycle, or None."""
    recent = list_recent(cycles_root, count=1)
    if not recent:
        return None
    fm = _read_frontmatter(recent[-1])
    if not fm:
        return None
    return str(fm.get("started") or "")


def _read_frontmatter(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return None
    try:
        import yaml
        return yaml.safe_load(m.group(1)) or {}
    except Exception:
        return None


def _yaml_escape(text: str) -> str:
    """Minimal YAML string escape — quote if contains special chars."""
    text = text or ""
    if any(c in text for c in (":", "#", '"', "'", "\n", "{", "}", "[", "]")):
        escaped = text.replace('"', '\\"').replace("\n", " ")
        return f'"{escaped}"'
    return text
