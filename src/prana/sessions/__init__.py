"""prana.sessions — the session manager.

Spawns, watches, and steers coding-agent sessions on this machine:

- owned sessions: spawned here via first-party subscription CLIs
  (``claude -p``, ``codex exec``, ``kimi``), each contained in a Windows
  Job Object and tracked through an explicit lifecycle in sessions.db
- foreign sessions: Claude Code sessions the human opened in their own
  terminals, discovered by scanning ``~/.claude/projects/**/*.jsonl``
- panes: owned sessions can be mirrored into wezterm panes for human
  watch-and-take-over

Exposed to cognition surfaces via the MCP server in :mod:`prana.sessions.mcp`
with caller-tier authorization: the voice tier reads and escalates; only
the prana tier mutates. See docs/plans/embodiment-rebirth-2026-08-06.md.
"""

from prana.sessions.registry import (  # noqa: F401
    Session,
    SessionState,
    SessionRegistry,
)
