"""Narada's MCP door to the box's screen (Suti 2026-09-10).

Launch (stdio, one process per client):

    python -m prana.box.mcp

Wired into every chat tier so "draw me a lemon and put it on the box"
works in one turn: the image lands in Codex's generated-images folder,
and ``box_show_image`` with no name picks the newest one.
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from prana.box import images

mcp = FastMCP("narada-box")


@mcp.tool()
async def box_show_image(name: str = "", ttl_s: int = 20) -> str:
    """Show a picture on the box's screen for ttl_s seconds (3–300).
    `name` is an image from the brain's store (the name in its
    /api/images/<name> url); leave it empty to show the picture you
    generated most recently. The result says "published": the box does
    not report back what it displayed — say "sent to the box", not
    "showing on the box", unless Suti confirms."""
    # async: FastMCP calls tools on its own event loop, where
    # asyncio.run() is refused (first live turn: "box connection error").
    try:
        return json.dumps(await images.show_image(name, ttl_s))
    except FileNotFoundError as exc:
        return json.dumps({"ok": False, "error": f"no such image: {exc}"})
    except Exception as exc:  # noqa: BLE001 — the tool reports, never raises
        return json.dumps({"ok": False, "error": f"could not reach the box: {exc}"})


@mcp.tool()
def box_list_images(limit: int = 12) -> str:
    """The most recent pictures in the brain's store, newest first."""
    folder = images.images_dir()
    if not folder.is_dir():
        return json.dumps([])
    files = sorted((p for p in folder.iterdir() if p.suffix.lower() in images.IMAGE_EXTS),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return json.dumps([{"name": p.name, "bytes": p.stat().st_size} for p in files[:max(1, limit)]])


if __name__ == "__main__":
    mcp.run()
