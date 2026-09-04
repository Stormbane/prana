"""Sessions — one warm agent client per conversation (spec §1a).

The pool owns every ``BrainSession``: creation (tier decides the wired
MCP subset), one-active-turn serialization, the global running-turn
cap, the idle reaper (closes the agent client, never the transcript),
and restart re-hydration via the backend-native session id.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

from prana.brain.config import TIER_SERVERS, BrainConfig
from prana.brain.turns import TurnStore

logger = logging.getLogger(__name__)

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

# Factory signature lets tests inject a FakeBackend; the server injects
# SdkBackend. kwargs: model, system_append, mcp_servers,
# max_tool_iterations, cwd, resume.
BackendFactory = Callable[..., object]


class SessionBusy(Exception):
    """A second turn arrived while one is active (contract: 409)."""


def valid_session_id(session_id: str) -> bool:
    return bool(_SESSION_ID_RE.match(session_id))


@dataclass
class TurnOutcome:
    text: str
    error: str | None = None  # bounds/backend failure — explicit, never dressed


class BrainSession:
    def __init__(self, session_id: str, tier: str, backend, store: TurnStore,
                 session_dir: Path):
        self.session_id = session_id
        self.tier = tier
        self.backend = backend
        self.turns = store
        self.session_dir = session_dir
        self.last_used = time.monotonic()
        self._turn_lock = asyncio.Lock()
        self._cancelled = False

    @property
    def busy(self) -> bool:
        return self._turn_lock.locked()

    async def run_turn(
        self, prompt: str, *, deadline_s: float,
        on_start: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncIterator[str]:
        """Yield assistant text; raise SessionBusy if a turn is active.

        The wall-clock bound ends the turn with an EXPLICIT error (the
        zombie-heartbeat rule): the caller sees a raised TimeoutError,
        never a silent truncation dressed as a completion.
        """
        if self._turn_lock.locked():
            raise SessionBusy(self.session_id)
        async with self._turn_lock:
            self._cancelled = False
            self.last_used = time.monotonic()
            if on_start is not None:
                await on_start()
            started = time.monotonic()
            agen = self.backend.run_turn(prompt)
            try:
                while True:
                    remaining = deadline_s - (time.monotonic() - started)
                    if remaining <= 0:
                        await self.backend.cancel()
                        raise TimeoutError(
                            f"turn exceeded {deadline_s:.0f}s wall-clock bound"
                        )
                    try:
                        chunk = await asyncio.wait_for(
                            agen.__anext__(), timeout=remaining
                        )
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        await self.backend.cancel()
                        raise TimeoutError(
                            f"turn exceeded {deadline_s:.0f}s wall-clock bound"
                        ) from None
                    yield chunk
            finally:
                await agen.aclose()
                self.last_used = time.monotonic()
                self._persist_native_id()

    async def cancel(self) -> bool:
        """Explicit cancel: stops at the next tool boundary. Returns
        whether a turn was actually in flight."""
        if not self._turn_lock.locked():
            return False
        self._cancelled = True
        await self.backend.cancel()
        return True

    @property
    def was_cancelled(self) -> bool:
        return self._cancelled

    def append_transcript(self, role: str, content: str) -> None:
        line = json.dumps(
            {"ts": time.time(), "role": role, "content": content},
            ensure_ascii=False,
        )
        path = self.session_dir / "transcript.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _persist_native_id(self) -> None:
        native = getattr(self.backend, "native_session_id", None)
        if native:
            (self.session_dir / "native-session-id").write_text(
                native, encoding="utf-8"
            )


class SessionPool:
    def __init__(self, config: BrainConfig, backend_factory: BackendFactory):
        self._config = config
        self._factory = backend_factory
        self._sessions: dict[str, BrainSession] = {}
        self._create_lock = asyncio.Lock()
        # Global cap on concurrently RUNNING turns, not on live sessions —
        # an idle warm session costs little; a running agent loop doesn't.
        self.turn_semaphore = asyncio.Semaphore(config.max_concurrent_sessions)
        self._reaper_task: asyncio.Task | None = None
        self._mcp_servers: dict | None = None

    def _tier_mcp(self, tier: str) -> dict:
        if self._mcp_servers is None:
            from prana.brain.config import _mcp_servers

            self._mcp_servers = _mcp_servers()
        allowed = TIER_SERVERS.get(tier, ())
        return {k: v for k, v in self._mcp_servers.items() if k in allowed}

    async def get_or_create(self, session_id: str, tier: str) -> BrainSession:
        """`session_id` is the client-chosen name; the pool key and the
        on-disk location are both namespaced by the authenticated tier
        (subdirectory, not `tier:` prefix — `:` is invalid in Windows
        dirnames), so one credential can never open another's session."""
        key = f"{tier}:{session_id}"
        async with self._create_lock:
            existing = self._sessions.get(key)
            if existing is not None:
                return existing

            session_dir = self._config.sessions_root / tier / session_id
            session_dir.mkdir(parents=True, exist_ok=True)
            resume = None
            native_file = session_dir / "native-session-id"
            if native_file.exists():
                resume = native_file.read_text(encoding="utf-8").strip() or None

            backend = self._factory(
                model=self._config.model,
                system_append=self._config.system_append(),
                mcp_servers=self._tier_mcp(tier),
                max_tool_iterations=self._config.max_tool_iterations,
                cwd=str(session_dir),
                resume=resume,
            )
            await backend.start()
            store = TurnStore(
                session_dir / "turns.json", window=self._config.idempotency_window
            )
            session = BrainSession(key, tier, backend, store, session_dir)
            self._sessions[key] = session
            logger.info(
                "session %s created (tier=%s, resume=%s)",
                key, tier, bool(resume),
            )
            return session

    def peek(self, session_id: str, tier: str) -> BrainSession | None:
        return self._sessions.get(f"{tier}:{session_id}")

    def start_reaper(self) -> None:
        self._reaper_task = asyncio.get_running_loop().create_task(self._reap())

    async def _reap(self) -> None:
        while True:
            await asyncio.sleep(60)
            cutoff = time.monotonic() - self._config.idle_ttl_s
            for sid, sess in list(self._sessions.items()):
                if sess.last_used < cutoff and not sess.busy:
                    logger.info("reaping idle session %s", sid)
                    del self._sessions[sid]
                    await sess.backend.close()

    async def close(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
        for sess in self._sessions.values():
            await sess.backend.close()
        self._sessions.clear()

    def stats(self) -> dict:
        return {
            "sessions": len(self._sessions),
            "busy": sum(1 for s in self._sessions.values() if s.busy),
        }
