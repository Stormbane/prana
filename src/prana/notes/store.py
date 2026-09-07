"""The notes store — a single JSON file, small and inspectable.

A note is either free text or a checklist (a shopping list is a
checklist named "Shopping"). Shape:

    {"id": "a1b2c3d4", "title": "Shopping", "kind": "checklist",
     "text": "", "items": [{"id": "…", "text": "milk", "done": false}],
     "created_at": iso, "updated_at": iso}

Writes are lock-guarded and atomic (tmp + replace) — the brain's REST
surface and an MCP server may write concurrently.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()
MAX_NOTES = 500


def _path() -> Path:
    return Path.home() / ".narada" / "brain" / "notes.json"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _load() -> list[dict]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save(notes: list[dict]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(notes[-MAX_NOTES:], indent=1), encoding="utf-8")
    tmp.replace(path)


def _new_id() -> str:
    return secrets.token_hex(4)


def list_notes() -> list[dict]:
    """All notes, most recently updated first."""
    return sorted(_load(), key=lambda n: n.get("updated_at", ""), reverse=True)


def get_note(note_id: str) -> dict | None:
    return next((n for n in _load() if n.get("id") == note_id), None)


def create_note(title: str, *, kind: str = "text", text: str = "",
                items: list[str] | None = None) -> dict:
    if kind not in ("text", "checklist"):
        raise ValueError("kind must be text or checklist")
    if not title.strip():
        raise ValueError("title is required")
    note = {
        "id": _new_id(),
        "title": title.strip()[:120],
        "kind": kind,
        "text": text,
        "items": [{"id": _new_id(), "text": t.strip(), "done": False}
                  for t in (items or []) if t.strip()],
        "created_at": _now(),
        "updated_at": _now(),
    }
    with _LOCK:
        notes = _load()
        notes.append(note)
        _save(notes)
    return note


def update_note(note_id: str, *, title: str | None = None,
                text: str | None = None,
                add_items: list[str] | None = None,
                remove_item_ids: list[str] | None = None,
                check: dict[str, bool] | None = None) -> dict:
    """Merge-style update; raises KeyError for an unknown note."""
    with _LOCK:
        notes = _load()
        note = next((n for n in notes if n.get("id") == note_id), None)
        if note is None:
            raise KeyError(note_id)
        if title is not None and title.strip():
            note["title"] = title.strip()[:120]
        if text is not None:
            note["text"] = text
        if add_items:
            note.setdefault("items", []).extend(
                {"id": _new_id(), "text": t.strip(), "done": False}
                for t in add_items if t.strip())
        if remove_item_ids:
            note["items"] = [i for i in note.get("items", [])
                             if i.get("id") not in set(remove_item_ids)]
        if check:
            for item in note.get("items", []):
                if item.get("id") in check:
                    item["done"] = bool(check[item["id"]])
        note["updated_at"] = _now()
        _save(notes)
        return note


def delete_note(note_id: str) -> bool:
    with _LOCK:
        notes = _load()
        kept = [n for n in notes if n.get("id") != note_id]
        if len(kept) == len(notes):
            return False
        _save(kept)
        return True
