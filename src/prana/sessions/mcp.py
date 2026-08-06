"""Local MCP server over the session manager — with caller tiers.

THE SOVEREIGNTY BOUNDARY LIVES HERE, IN CODE. The realtime voice model's
system prompt is UX, not security; what a caller can do is decided by
which tier this server process was launched as, verified by a token.

Tiers:
- ``voice``  — read + escalate only. list/status/output/panes-focus,
  file proposals for mutations, check proposal status. NO direct
  spawn/relay/cancel, NO foreign-session resumption, ever.
- ``prana``  — full surface. This tier is reachable only from
  judgment-bearing surfaces: the chat bridge (Suti's own messages from
  an allowlisted Telegram chat) and Narada-in-claude.

Launch (one process per client, stdio transport):

    python -m prana.sessions.mcp --tier voice
    python -m prana.sessions.mcp --tier prana

The launching surface must put the matching token in the
``PRANA_SESSIONS_TOKEN`` env var. Tokens live in
``~/.narada/.sessions-tokens.json`` (created on first prana-tier run;
chmod-equivalent protection is the user profile ACL).

v1 confirmation channel for ``resume_foreign_session``: the prana tier
itself — a request arriving there originates from Suti's allowlisted
Telegram chat or a local shell, which is the authenticated,
server-verifiable channel the plan requires. A voice acknowledgement
can never reach this tool: it does not exist in the voice tier's
registry at all.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import sys
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from prana.sessions import panes, watcher
from prana.sessions.escalate import ProposalQueue, ProposalError, judge_with_narada
from prana.sessions.manager import ManagerConfig, SessionManager

logger = logging.getLogger(__name__)

TOKENS_FILE = Path.home() / ".narada" / ".sessions-tokens.json"
TIERS = ("voice", "prana")


def _load_or_create_tokens() -> dict[str, str]:
    if TOKENS_FILE.exists():
        try:
            return json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            logger.warning("tokens file unreadable; regenerating")
    tokens = {tier: secrets.token_urlsafe(32) for tier in TIERS}
    TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKENS_FILE.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
    logger.info("wrote new tier tokens to %s", TOKENS_FILE)
    return tokens


def _authenticate(tier: str) -> None:
    """Refuse to serve a tier whose token the launcher doesn't hold."""
    presented = os.environ.get("PRANA_SESSIONS_TOKEN", "")
    expected = _load_or_create_tokens().get(tier, "")
    if not presented or not secrets.compare_digest(presented, expected):
        raise SystemExit(
            f"PRANA_SESSIONS_TOKEN missing or wrong for tier {tier!r}; "
            f"see {TOKENS_FILE}"
        )


def _session_dict(s) -> dict:
    return {
        "id": s.id, "provider": s.provider, "cwd": s.cwd, "title": s.title,
        "state": s.state.value, "pane_id": s.pane_id,
        "provider_session_id": s.provider_session_id,
        "last_activity_at": s.last_activity_at, "last_error": s.last_error,
    }


def build_server(tier: str, manager: Optional[SessionManager] = None) -> FastMCP:
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}")
    mgr = manager or SessionManager(ManagerConfig())
    proposals = ProposalQueue(mgr.config.db_path)
    server = FastMCP(f"narada-sessions-{tier}")

    # ── read surface (both tiers) ────────────────────────────────────

    @server.tool()
    def list_sessions(live_only: bool = True) -> list[dict]:
        """List owned coding-agent sessions and their lifecycle state."""
        mgr.sweep()
        return [_session_dict(s) for s in mgr.list_sessions(live_only=live_only)]

    @server.tool()
    def session_status(session_id: str) -> dict:
        """Status of one owned session."""
        return _session_dict(mgr.get(session_id))

    @server.tool()
    def read_output(session_id: str, limit: int = 50) -> list[str]:
        """Recent output lines from an owned session."""
        return mgr.recent_output(session_id, limit=limit)

    @server.tool()
    def list_foreign_sessions() -> list[dict]:
        """Claude Code sessions visible on this machine (any terminal),
        newest first: id, project, active?, last speaker, summary."""
        return [
            {
                "session_id": f.session_id, "project": f.project_dir,
                "active": f.active, "last_role": f.last_role,
                "summary": f.summary,
                "mtime": f.mtime.isoformat(),
            }
            for f in watcher.scan()
        ]

    @server.tool()
    def focus_pane(session_id: str) -> bool:
        """Bring an owned session's wezterm pane to front (UI focus only)."""
        sess = mgr.get(session_id)
        if not sess.pane_id:
            return False
        return panes.focus_pane(sess.pane_id)

    @server.tool()
    def proposal_status(proposal_id: int) -> dict:
        """Check the status of a filed mutation proposal."""
        p = proposals.get(proposal_id)
        return {"id": p.id, "tool": p.tool, "status": p.status,
                "decided_by": p.decided_by, "reason": p.decision_reason}

    # ── voice tier: escalation instead of mutation ───────────────────

    if tier == "voice":

        @server.tool()
        def escalate_to_narada(tool: str, params: dict, context: str = "") -> dict:
            """Request a mutation (spawn_session / relay_instruction /
            cancel_session). Files a durable proposal and asks Narada's
            judgment; returns the decision. Rejection is normal — say so
            honestly and offer to ask Suti instead."""
            try:
                p = proposals.propose("voice", tool, params)
            except ProposalError as exc:
                return {"approved": False, "reason": str(exc)}
            approve, reason = judge_with_narada(p, context=context)
            p = proposals.decide(
                p.id, approve=approve, decided_by="narada-judge", reason=reason
            )
            if not approve:
                return {"proposal_id": p.id, "approved": False, "reason": reason}
            executed = _execute(p.tool, p.params)
            proposals.redeem(p.id, p.capability or "")
            return {"proposal_id": p.id, "approved": True,
                    "reason": reason, "result": executed}

    # ── prana tier: direct mutations ─────────────────────────────────

    if tier == "prana":

        @server.tool()
        def spawn_session(
            provider: str, cwd: str, prompt: str,
            idempotency_key: str = "", title: str = "",
        ) -> dict:
            """Spawn a coding-agent session (claude | codex | kimi)."""
            sess = mgr.spawn(
                provider, cwd, prompt, title=title,
                idempotency_key=idempotency_key or None,
            )
            return _session_dict(sess)

        @server.tool()
        def relay_instruction(session_id: str, text: str) -> bool:
            """Send a follow-up instruction into a live owned session."""
            return mgr.relay(session_id, text)

        @server.tool()
        def cancel_session(session_id: str) -> dict:
            """Kill an owned session and its whole process tree."""
            return _session_dict(mgr.cancel(session_id))

        @server.tool()
        def decide_proposal(
            proposal_id: int, approve: bool, reason: str = "",
        ) -> dict:
            """Suti/prana decides a pending voice proposal by hand."""
            p = proposals.decide(
                proposal_id, approve=approve, decided_by="prana",
                reason=reason,
            )
            if approve:
                result = _execute(p.tool, p.params)
                proposals.redeem(p.id, p.capability or "")
                return {"id": p.id, "status": "executed", "result": result}
            return {"id": p.id, "status": p.status}

        @server.tool()
        def resume_foreign_session(session_id: str, prompt: str) -> dict:
            """Continue a foreign Claude Code session under our management.
            Prana-tier only: the request itself arrives over the
            authenticated channel (Suti's allowlisted chat / local shell) —
            that IS the confirmation. Registers the session as owned."""
            sess = mgr.spawn(
                "claude", str(Path.home()), prompt,
                title=f"resumed:{session_id[:8]}",
                resume_session_id=session_id,
            )
            return _session_dict(sess)

    def _execute(tool: str, params: dict):
        if tool == "spawn_session":
            return _session_dict(mgr.spawn(
                params["provider"], params["cwd"], params["prompt"],
                title=params.get("title", ""),
                idempotency_key=params.get("idempotency_key") or None,
            ))
        if tool == "relay_instruction":
            return mgr.relay(params["session_id"], params["text"])
        if tool == "cancel_session":
            return _session_dict(mgr.cancel(params["session_id"]))
        raise ProposalError(f"unknown tool {tool!r}")

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="narada session-manager MCP")
    parser.add_argument("--tier", choices=TIERS, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    _authenticate(args.tier)
    server = build_server(args.tier)
    server.run()  # stdio transport


if __name__ == "__main__":
    main()
