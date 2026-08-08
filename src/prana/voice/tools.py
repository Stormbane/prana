"""Voice-tier function tools for the realtime model.

THIS LIST IS THE SOVEREIGNTY BOUNDARY at the voice surface: the realtime
model can call exactly these tools and nothing else. Reads go straight
through; the single mutation path (`request_session_action`) files a
durable proposal and asks Narada's judgment (fail-closed). There is no
spawn/relay/cancel/resume tool here, and none may ever be added without
revisiting docs/plans/embodiment-rebirth-2026-08-06.md §Phase 1.

Mirrors the voice-tier MCP registry (prana.sessions.mcp) — same
underlying modules, LiveKit function-tool dressing.
"""

from __future__ import annotations

import logging
from typing import Annotated, Optional

from livekit.agents import function_tool

from prana.sessions import watcher
from prana.sessions.escalate import ProposalError, ProposalQueue, judge_with_narada
from prana.sessions.service import ServiceClient, ServiceUnavailable
from prana.voice import memory
from prana.voice.escalate import escalate

logger = logging.getLogger(__name__)


def build_voice_tools(
    client: Optional[ServiceClient] = None,
    proposals: Optional[ProposalQueue] = None,
) -> list:
    """Build the closed voice-tier tool list for an AgentSession."""
    client = client or ServiceClient()
    proposals = proposals or ProposalQueue()

    @function_tool()
    async def list_coding_sessions() -> list[dict]:
        """List the coding-agent sessions Narada owns on this machine,
        with lifecycle state (running / idle / done / hung)."""
        try:
            client.sweep()
            return client.list_sessions(live_only=False)[-20:]
        except ServiceUnavailable as exc:
            return [{"error": str(exc)}]

    @function_tool()
    async def list_open_terminals() -> list[dict]:
        """List Claude Code sessions visible on this machine — including
        ones Suti opened himself — newest first, with a summary of what
        each is doing."""
        return [
            {
                "session_id": f.session_id, "project": f.project_dir,
                "active": f.active, "last_role": f.last_role,
                "summary": f.summary,
            }
            for f in watcher.scan()[:15]
        ]

    @function_tool()
    async def read_session_output(
        session_id: Annotated[str, "id from list_coding_sessions"],
    ) -> list[str]:
        """Recent output from an owned coding session."""
        try:
            return client.recent_output(session_id, limit=30)
        except (ServiceUnavailable, RuntimeError) as exc:
            return [f"(unavailable: {exc})"]

    @function_tool()
    async def request_session_action(
        tool: Annotated[str, "spawn_session | relay_instruction | cancel_session"],
        params: Annotated[dict, "parameters for the requested action"],
        context: Annotated[str, "why — quoted from what Suti actually said"],
    ) -> dict:
        """Request a coding-session mutation. Narada's judgment decides;
        rejection is normal — report it honestly and offer to have Suti
        do it from chat instead."""
        try:
            proposal = proposals.propose("voice", tool, params)
        except ProposalError as exc:
            return {"approved": False, "reason": str(exc)}
        approve, reason = judge_with_narada(proposal, context=context)
        proposal = proposals.decide(
            proposal.id, approve=approve, decided_by="narada-judge",
            reason=reason,
        )
        if not approve:
            return {"approved": False, "reason": reason,
                    "proposal_id": proposal.id}
        # REDEEM FIRST: the single-use capability is the gate, not the
        # receipt. A crash after redeem loses the action (safe); the
        # old order could execute a mutation whose capability was never
        # validated, and retry it after a redeem failure (unsafe).
        try:
            proposals.redeem(proposal.id, proposal.capability or "")
        except ProposalError as exc:
            return {"approved": True, "executed": False,
                    "reason": f"capability not redeemable: {exc}"}
        try:
            if tool == "spawn_session":
                result = client.spawn(
                    params["provider"], params["cwd"], params["prompt"],
                    title=params.get("title", ""),
                    # proposal-derived key: an accidental retry of the
                    # same approved action can never double-spawn
                    idempotency_key=f"proposal-{proposal.id}",
                )
            elif tool == "relay_instruction":
                result = client.relay(params["session_id"], params["text"])
            else:
                result = client.cancel(params["session_id"])
            return {"approved": True, "reason": reason, "result": result}
        except (ServiceUnavailable, RuntimeError, KeyError) as exc:
            return {"approved": True, "executed": False, "error": str(exc)}

    @function_tool()
    async def recall_memory(
        query: Annotated[str, "what to recall, in a few words"],
    ) -> list[dict]:
        """Recall shareable notes from Narada's memory. Voice-safe by
        construction: only non-private branches are searched — personal,
        journal, and identity memory are never accessible here."""
        return [
            {"branch": m.branch, "note": m.snippet}
            for m in memory.recall(query)
        ]

    @function_tool()
    async def escalate_to_narada(
        question: Annotated[str, "the question, quoted from what Suti asked"],
    ) -> dict:
        """Hand a substantive question (judgment, tradeoffs, explanation)
        to Narada's deeper reasoning. Slower than a quick reply — say
        'let me think about that properly' first. Cannot touch files or
        make changes; those go through Suti's authenticated channels."""
        answer = await escalate(question)
        return {"answer": answer}

    return [
        list_coding_sessions,
        list_open_terminals,
        read_session_output,
        request_session_action,
        recall_memory,
        escalate_to_narada,
    ]
