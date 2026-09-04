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
    ):
        # Import here so the API layer stays importable (and testable
        # with FakeBackend) on machines without the SDK.
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        self._options = ClaudeAgentOptions(
            system_prompt={"type": "preset", "preset": "claude_code",
                           "append": system_append},
            model=model,
            mcp_servers=mcp_servers,
            permission_mode="bypassPermissions",
            max_turns=max_tool_iterations,
            cwd=cwd,
            resume=resume,
        )
        self._client = ClaudeSDKClient(options=self._options)
        self._native_session_id: str | None = resume

    async def start(self) -> None:
        await self._client.connect()

    async def run_turn(self, prompt: str) -> AsyncIterator[str]:
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

        await self._client.query(prompt)
        async for msg in self._client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        yield block.text
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
                break

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
