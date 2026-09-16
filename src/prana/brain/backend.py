"""Agent backends — the internal fork the spec allows.

The outer HTTP/session/auth/turn contract never sees which backend
runs. ``SdkBackend`` (ruled default, validated by the 2026-09-05 spike:
2.0s warm vs 6.7s cold) holds one ``ClaudeSDKClient`` per session with
MCP connections open. The Messages-API loop is the recorded fallback
and would implement this same protocol.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator, Protocol

logger = logging.getLogger(__name__)


class AgentBackend(Protocol):
    async def start(self) -> None: ...

    def run_turn(self, prompt: str) -> AsyncIterator[str]:
        """Run one agentic turn; yield assistant text as it arrives."""
        ...

    async def cancel(self) -> None:
        """Stop the in-flight turn at the next tool boundary."""
        ...

    async def close(self) -> None: ...

    @property
    def native_session_id(self) -> str | None:
        """Backend-native transcript id (CLI session id) for resume."""
        ...


class SdkBackend:
    def __init__(
        self,
        *,
        model: str,
        system_append: str,
        mcp_servers: dict,
        max_tool_iterations: int,
        cwd: str,
        resume: str | None = None,
        disallowed_tools: list[str] | None = None,
        handover: str | None = None,
    ):
        # Import here so the API layer stays importable (and testable
        # with FakeBackend) on machines without the SDK.
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        # A conversation carried over from another backend rides the
        # first turn as a context block (registry, 2026-09-09).
        self._handover = handover

        self._options = ClaudeAgentOptions(
            system_prompt={"type": "preset", "preset": "claude_code",
                           "append": system_append},
            model=model,
            mcp_servers=mcp_servers,
            # ONLY the servers the tier wires. Without this the CLI also
            # loads the user's own MCP config — where an "akhada" entry
            # with a bare "python" died under the supervisor's PATH and
            # took the name with it ("failed to connect", 2026-09-16),
            # plus every connector and plugin server the phone never
            # needs, each one a slower session start.
            strict_mcp_config=True,
            permission_mode="bypassPermissions",
            max_turns=max_tool_iterations,
            cwd=cwd,
            resume=resume,
            # The phone chat records code changes rather than making
            # them (charter.py); the editing tools are off at the SDK
            # too, so the rule has teeth.
            disallowed_tools=list(disallowed_tools or []),
        )
        self._client = ClaudeSDKClient(options=self._options)
        self._native_session_id: str | None = resume

    async def start(self) -> None:
        await self._client.connect()

    async def run_turn(self, prompt) -> AsyncIterator[str]:
        """`prompt`: a plain string, or a list of Claude content blocks
        (text + base64 image blocks) for multimodal turns."""
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

        if self._handover:
            preamble = self._handover.rstrip() + "\n\n"
            if isinstance(prompt, str):
                prompt = preamble + prompt
            elif isinstance(prompt, list):
                prompt = [{"type": "text", "text": preamble}, *prompt]
        if isinstance(prompt, str):
            await self._client.query(prompt)
        else:
            async def _one_message():
                yield {
                    "type": "user",
                    "message": {"role": "user", "content": prompt},
                    "parent_tool_use_id": None,
                    "session_id": "default",
                }
            await self._client.query(_one_message())
        from claude_agent_sdk import ToolResultBlock, ToolUseBlock, UserMessage

        from prana.brain.cards import ToolTracker

        # Auto-cards (plan §T4) and activity chips (Suti 2026-09-08/09):
        # the SDK streams every tool use and its result; the tracker
        # pairs them — a chip the moment a call starts, then the card
        # (if the result mints one) and the chip's closing state, in
        # place between the text they accompany.
        tracker = ToolTracker()
        concluded = False
        async for msg in self._client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        yield block.text
                    elif isinstance(block, ToolUseBlock):
                        for ev in tracker.on_use(block.id, block.name, block.input):
                            yield ev
            elif isinstance(msg, UserMessage):
                content = msg.content if isinstance(msg.content, list) else []
                for block in content:
                    if not isinstance(block, ToolResultBlock):
                        continue
                    for ev in tracker.on_result(
                            block.tool_use_id, block.content,
                            bool(getattr(block, "is_error", False))):
                        yield ev
            elif isinstance(msg, ResultMessage):
                sid = getattr(msg, "session_id", None)
                if sid:
                    self._native_session_id = sid
                # The SDK reports max_turns / API errors as a result
                # with is_error set rather than raising. Ending the
                # iterator normally here would let partial text be
                # persisted as a `completed` turn — the masquerade the
                # house rules forbid. Raise so the turn records failed.
                if getattr(msg, "is_error", False):
                    subtype = getattr(msg, "subtype", "unknown")
                    raise RuntimeError(
                        f"agent turn ended in error (subtype={subtype})"
                    )
                self._handover = None  # delivered (review: not before)
                concluded = True
                break
        if not concluded:
            # A clean transport EOF without a result is not a completed
            # turn (recheck): ending normally here would have retired a
            # pending handover and persisted partial text as complete.
            raise RuntimeError("agent stream ended without a result")

    async def failed_mcp_servers(self) -> dict[str, str]:
        """{name: why} for the tool servers the CLI reports failed —
        the check the session pool runs right after start (Suti
        2026-09-16: "why didn't we know it was down?"). Empty when the
        status itself cannot be read: unknown is not failed."""
        import asyncio

        try:
            status = await asyncio.wait_for(self._client.get_mcp_status(), timeout=15.0)
        except Exception as exc:  # noqa: BLE001 — a status hiccup must not fail the session
            logger.warning("tool-server status unavailable: %s", exc)
            return {}
        return {
            str(s.get("name")): str(s.get("error") or "failed")
            for s in status.get("mcpServers", [])
            if s.get("status") == "failed"
        }

    async def cancel(self) -> None:
        await self._client.interrupt()

    async def close(self) -> None:
        try:
            await self._client.disconnect()
        except Exception:  # closing a dead client must never raise upward
            logger.warning("backend close failed", exc_info=True)

    @property
    def native_session_id(self) -> str | None:
        return self._native_session_id
