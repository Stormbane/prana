"""Narada's MCP door to the notes store.

Launch (stdio, one process per client):

    python -m prana.notes.mcp

Registered in the brain's MCP wiring so every chat session — phone app
included — can read Suti's notes, add to the shopping list, or check
items off. Same JSON store the app's Data tab edits.
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from prana.notes import store

mcp = FastMCP("narada-notes")


@mcp.tool()
def notes_list() -> str:
    """List Suti's notes (title, kind, id, item counts), newest first."""
    out = []
    for n in store.list_notes():
        items = n.get("items", [])
        done = sum(1 for i in items if i.get("done"))
        out.append({"id": n["id"], "title": n["title"], "kind": n["kind"],
                    "updated_at": n.get("updated_at", ""),
                    **({"items_done": f"{done}/{len(items)}"} if items else {})})
    return json.dumps(out)


@mcp.tool()
def notes_get(note_id: str) -> str:
    """Read one note in full — text, and checklist items with ids."""
    note = store.get_note(note_id)
    return json.dumps(note) if note else json.dumps({"error": "no such note"})


@mcp.tool()
def notes_create(title: str, kind: str = "text", text: str = "",
                 items: list[str] | None = None) -> str:
    """Create a note. kind 'text' (free text) or 'checklist' (e.g. a
    shopping list — pass items as a list of strings)."""
    try:
        return json.dumps(store.create_note(title, kind=kind, text=text,
                                            items=items))
    except ValueError as exc:
        return json.dumps({"error": str(exc)})


@mcp.tool()
def notes_update(note_id: str, title: str | None = None,
                 text: str | None = None,
                 add_items: list[str] | None = None) -> str:
    """Update a note's title or text, or append checklist items."""
    try:
        return json.dumps(store.update_note(
            note_id, title=title, text=text, add_items=add_items))
    except KeyError:
        return json.dumps({"error": "no such note"})


@mcp.tool()
def notes_check_item(note_id: str, item_id: str, done: bool = True) -> str:
    """Check (or uncheck) one checklist item — 'milk is done'."""
    try:
        return json.dumps(store.update_note(note_id, check={item_id: done}))
    except KeyError:
        return json.dumps({"error": "no such note"})


@mcp.tool()
def notes_delete(note_id: str) -> str:
    """Delete a note permanently. Only when Suti asks."""
    return json.dumps({"deleted": store.delete_note(note_id)})


if __name__ == "__main__":
    mcp.run()
