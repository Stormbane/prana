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
from prana.voice import memory, messaging, remember, timers, web
from prana.voice.escalate import escalate

logger = logging.getLogger(__name__)


def build_voice_tools(
    client: Optional[ServiceClient] = None,
    proposals: Optional[ProposalQueue] = None,
    tier: str = "shareable",
    session_id: str = "",
    music=None,
    publish=None,
    end_session=None,
) -> list:
    """Build the closed voice-tier tool list for an AgentSession.

    `tier` and `session_id` come from the worker's admission state and
    are stamped onto anything the session writes. `music` is the job's
    MusicPlayer (audio-owner state machine) or None."""
    client = client or ServiceClient()
    proposals = proposals or ProposalQueue()

    def _toast(icon: str, text: str) -> None:
        """Activity pill on the face (Suti's overlay design). Fire and
        forget — candy never blocks a tool."""
        if publish is None:
            return
        import asyncio as _aio
        from prana.voice.admission import TOPIC_SESSION
        try:
            _aio.ensure_future(publish(
                TOPIC_SESSION,
                {"type": "activity", "icon": icon, "text": text[:72]}))
        except Exception:
            pass

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
    async def remember_this(
        note: Annotated[str, "the thing worth keeping, one or two sentences"],
    ) -> dict:
        """Save a note to Narada's memory inbox. Use when Suti says
        'remember that' or something is clearly worth keeping. Say aloud
        that you noted it. Notes go to a review inbox — Narada curates
        them into memory later, so don't promise it's remembered
        forever, just that it's written down."""
        _toast("memory", "writing to memory")
        try:
            remember.write_note(note, tier=tier, session_id=session_id)
            return {"saved": True}
        except remember.QuotaExceeded as exc:
            return {"saved": False, "reason": str(exc)}
        except ValueError as exc:
            return {"saved": False, "reason": str(exc)}

    @function_tool()
    async def message_suti(
        text: Annotated[str, "the message, short and worth interrupting him for"],
    ) -> dict:
        """Send Suti a Telegram message. Personal tier only, rate
        capped. Use for things he'd want to know while away — not
        chatter. Report a failed delivery honestly."""
        try:
            return messaging.send_to_suti(
                text, tier=tier, session_id=session_id)
        except (messaging.NotAllowed, messaging.RateLimited,
                ValueError) as exc:
            return {"delivered": False, "detail": str(exc)}

    @function_tool()
    async def end_conversation() -> dict:
        """End this conversation and go back to sleep. Call when Suti
        says stop / that's all / goodnight — say a brief goodbye FIRST,
        then call this."""
        if end_session is None:
            return {"ended": False, "reason": "not available in this mode"}
        end_session()
        return {"ended": True}

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

    @function_tool()
    async def play_music(
        station: Annotated[str, "station name, or part of one"],
    ) -> dict:
        """Play a radio station on the body's speaker. During a
        conversation it queues and starts when you finish talking —
        say so. Music pauses whenever a conversation starts."""
        if music is None:
            return {"playing": False, "reason": "no player in this mode"}
        _toast("music", f"radio: {station}")
        return await music.play(station)

    @function_tool()
    async def stop_music() -> dict:
        """Stop the music."""
        if music is None:
            return {"stopped": False, "reason": "no player in this mode"}
        return await music.stop()

    @function_tool()
    async def what_is_playing() -> dict:
        """What's playing (or queued), volume, and any stream error."""
        if music is None:
            return {"state": "unavailable"}
        return music.now_playing()

    @function_tool()
    async def set_volume(
        percent: Annotated[int, "speaker loudness, 0-100"],
    ) -> dict:
        """Set the box's SPEAKER volume — how loud everything is,
        voice and music alike. This is the one to use when asked to
        turn it up or down."""
        if publish is None:
            return {"set": False, "reason": "no device channel in this mode"}
        from prana.voice.admission import TOPIC_SESSION
        pct = max(0, min(100, int(percent)))
        await publish(TOPIC_SESSION, {"type": "set_volume", "volume": pct})
        _toast("volume", f"volume {pct}%")
        return {"set": True, "volume": pct}

    @function_tool()
    async def set_music_volume(
        percent: Annotated[int, "0-100"],
    ) -> dict:
        """Set the music MIX level relative to speech (not overall
        loudness — for that use set_volume)."""
        if music is None:
            return {"volume": None, "reason": "no player in this mode"}
        return music.set_volume(percent)

    @function_tool()
    async def web_search(
        query: Annotated[str, "what to look up, a few words"],
    ) -> list[dict]:
        """Search the web. Returns a few titled results with snippets.
        If search isn't available, say so plainly."""
        import asyncio as _aio
        _toast("search", f"searching: {query}")
        try:
            return await _aio.to_thread(web.search, query)
        except (web.WebUnavailable, web.WebRefused) as exc:
            return [{"error": str(exc)}]

    @function_tool()
    async def read_page(
        url: Annotated[str, "a result url from web_search"],
    ) -> dict:
        """Read a public web page (text only, truncated). Local and
        private addresses are refused by design."""
        import asyncio as _aio
        from urllib.parse import urlsplit as _us
        try:
            _host = _us(url).hostname or "a page"
        except ValueError:
            _host = "a page"
        _toast("web", f"reading {_host}")
        try:
            text = await _aio.to_thread(web.fetch, url)
            return {"text": text}
        except (web.WebUnavailable, web.WebRefused) as exc:
            return {"error": str(exc)}

    # Music and web are shareable-tier by ratified decision
    # (guest-tolerable by the wall-calendar rule).
    tools = [
        list_coding_sessions,
        list_open_terminals,
        read_session_output,
        request_session_action,
        recall_memory,
        remember_this,
        escalate_to_narada,
        end_conversation,
        play_music,
        stop_music,
        what_is_playing,
        set_volume,
        set_music_volume,
        web_search,
        read_page,
    ]
    @function_tool()
    async def set_timer(
        minutes: Annotated[float, "how long from now, in minutes"],
        label: Annotated[str, "what it's for, a few words"],
    ) -> dict:
        """Set a timer. It reaches Suti's Telegram when it fires."""
        _toast("timer", f"timer: {label}")
        try:
            r = timers.create(label, minutes * 60.0, kind="timer",
                              tier=tier, session_id=session_id)
            return {"set": True, "id": r["id"]}
        except timers.TimerError as exc:
            return {"set": False, "reason": str(exc)}

    @function_tool()
    async def set_reminder(
        hours: Annotated[float, "how long from now, in hours"],
        text: Annotated[str, "what to remind Suti about"],
    ) -> dict:
        """Set a reminder (up to 14 days out). Delivered to Suti's
        Telegram when due."""
        try:
            r = timers.create(text, hours * 3600.0, kind="reminder",
                              tier=tier, session_id=session_id)
            return {"set": True, "id": r["id"]}
        except timers.TimerError as exc:
            return {"set": False, "reason": str(exc)}

    @function_tool()
    async def list_timers() -> list[dict]:
        """List pending timers and reminders."""
        return timers.list_pending()

    @function_tool()
    async def cancel_timer(
        timer_id: Annotated[int, "id from list_timers"],
    ) -> dict:
        """Cancel a pending timer or reminder."""
        try:
            ok = timers.cancel(timer_id, tier=tier)
            return {"cancelled": ok}
        except timers.TimerError as exc:
            return {"cancelled": False, "reason": str(exc)}

    # message_suti and the timer tools exist ONLY on the personal
    # surface (B2/C1): a shareable session does not carry them, and the
    # underlying modules re-check the tier in code besides. Timers open
    # to the shareable tier (local-only chime, no Telegram) once B5's
    # audio owner gives the body a local announcement channel.
    if tier == "personal":
        tools.extend([message_suti, set_timer, set_reminder,
                      list_timers, cancel_timer])
    return tools
