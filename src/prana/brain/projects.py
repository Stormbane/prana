"""Projects overview for the app's Data tab (plan §T1 "projects card").

Two read-only sources, both already maintained by the memory system:
``~/.narada/mirrors/<name>/ai/todo.md`` (the in-repo roadmap — open /
done checkboxes, current milestone heading) and
``~/.narada/projects/<name>/<yyyy>/<mm-dd>.md`` (smriti's project
notes — the newest one's date and title say where things stand).
Nothing here writes.
"""

from __future__ import annotations

import re
from pathlib import Path

_OPEN = re.compile(r"^\s*- \[ \]", re.M)
_DONE = re.compile(r"^\s*- \[x\]", re.M | re.I)
_MILESTONE = re.compile(r"^## (.+)$", re.M)
_TITLE = re.compile(r"^# (.+)$", re.M)
_NOTE = re.compile(r"^(\d{2})-(\d{2})\.md$")


def _root() -> Path:
    return Path.home() / ".narada"


def _todo(name: str) -> dict:
    path = _root() / "mirrors" / name / "ai" / "todo.md"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"todo_open": None, "todo_done": None, "milestone": None}
    milestones = _MILESTONE.findall(text)
    return {
        "todo_open": len(_OPEN.findall(text)),
        "todo_done": len(_DONE.findall(text)),
        "milestone": milestones[0].strip() if milestones else None,
    }


def _latest_note(name: str) -> dict:
    base = _root() / "projects" / name
    newest: tuple[str, Path] | None = None
    try:
        for year in base.iterdir():
            if not year.is_dir() or not year.name.isdigit():
                continue
            for f in year.iterdir():
                m = _NOTE.match(f.name)
                if not m:
                    continue
                key = f"{year.name}-{m.group(1)}-{m.group(2)}"
                if newest is None or key > newest[0]:
                    newest = (key, f)
    except OSError:
        return {"last_note_at": None, "last_note_title": None}
    if newest is None:
        return {"last_note_at": None, "last_note_title": None}
    try:
        text = newest[1].read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"last_note_at": newest[0], "last_note_title": None}
    # Notes append through the day; the LAST H1 is the newest entry.
    titles = _TITLE.findall(text)
    return {"last_note_at": newest[0],
            "last_note_title": titles[-1].strip() if titles else None}


def list_projects() -> list[dict]:
    names: set[str] = set()
    for sub in ("mirrors", "projects"):
        try:
            for p in (_root() / sub).iterdir():
                if p.is_dir() and not p.name.startswith("."):
                    names.add(p.name)
        except OSError:
            continue
    out = []
    for name in sorted(names):
        row = {"name": name, **_todo(name), **_latest_note(name)}
        out.append(row)
    out.sort(key=lambda r: (r.get("last_note_at") or "", r["name"]), reverse=True)
    return out
