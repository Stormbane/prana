"""Narada's MCP door to the phone's own chat transcripts (Suti
2026-09-21: "make it so the chat mirror stuff can happen when
requested (tool call), not just automatically").

Launch (stdio, one process per client):

    python -m prana.chats.mcp

The phone mirrors each conversation's local transcript to the brain
(``PUT /api/chats/{id}``): before every turn it sends, a few seconds
after any change, and for every conversation when it comes back online.
So by the time a turn runs, the conversation it came from is current
here, and messages typed while the phone could not connect are here
too, marked by their failed replies. These tools read that mirror.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("narada-chats")

TIERS = ("app", "prana", "voice")


def chats_root() -> Path:
    return Path(os.environ.get("NARADA_BRAIN_CHATS_ROOT")
                or Path.home() / ".narada" / "brain" / "chats")


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _find(chat_id: str) -> Path | None:
    if not chat_id or "/" in chat_id or "\\" in chat_id or ".." in chat_id:
        return None
    for tier in TIERS:
        p = chats_root() / tier / f"{chat_id}.json"
        if p.is_file():
            return p
    return None


@mcp.tool()
def list_chats(limit: int = 20) -> str:
    """The phone's chats as mirrored here, newest first: id, title, when
    the phone last updated it, when it was mirrored, how many messages,
    and how many turns failed to send. Use it to find the chat Suti
    means ("the one where I tried to log exercises")."""
    rows = []
    for tier in TIERS:
        folder = chats_root() / tier
        if not folder.is_dir():
            continue
        for p in folder.glob("*.json"):
            doc = _load(p)
            if doc is None:
                continue
            msgs = doc.get("messages") or []
            rows.append({
                "id": doc.get("id", p.stem), "tier": tier, "title": doc.get("title", ""),
                "updatedAt": doc.get("updatedAt"), "mirrored_at": doc.get("mirrored_at"),
                "messages": len(msgs),
                "failed_turns": sum(1 for m in msgs if m.get("status") == "error"),
            })
    rows.sort(key=lambda r: r.get("updatedAt") or 0, reverse=True)
    return json.dumps(rows[:max(1, min(int(limit), 100))], ensure_ascii=False)


@mcp.tool()
def read_chat(chat_id: str, last_n: int = 40, failed_only: bool = False) -> str:
    """One mirrored chat's messages (the last `last_n`, oldest first):
    role, text, status, and the error a reply failed with. `failed_only`
    keeps just the user messages whose reply failed to send — the ones
    that never reached the brain — with the reply's error. What the
    phone holds, mirrored; not the brain's own transcript."""
    p = _find(chat_id)
    if p is None:
        return json.dumps({"ok": False, "error": f"no mirrored chat {chat_id!r} — list_chats first"})
    doc = _load(p)
    if doc is None:
        return json.dumps({"ok": False, "error": "the mirror file is unreadable"})
    msgs = doc.get("messages") or []
    if failed_only:
        out = []
        for i, m in enumerate(msgs):
            if m.get("role") == "assistant" and m.get("status") == "error":
                prev = next((u for u in reversed(msgs[:i]) if u.get("role") == "user"), None)
                if prev is not None:
                    out.append({"user": prev.get("text", ""), "error": m.get("errorDetail", "")})
        return json.dumps({"ok": True, "id": doc.get("id"), "title": doc.get("title", ""),
                           "failed": out}, ensure_ascii=False)
    n = max(1, min(int(last_n), 500))
    return json.dumps({
        "ok": True, "id": doc.get("id"), "title": doc.get("title", ""),
        "mirrored_at": doc.get("mirrored_at"), "total": len(msgs),
        "messages": [{k: v for k, v in m.items() if k in ("id", "role", "text", "status", "errorDetail", "images")}
                     for m in msgs[-n:]],
    }, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
