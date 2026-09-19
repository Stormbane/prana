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

from prana.brain import fitness
from prana.brain.config import TIER_SERVERS, BrainConfig, backend_for
from prana.brain.turns import TurnStore

logger = logging.getLogger(__name__)

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

# Factory signature lets tests inject a FakeBackend; the server injects
# SdkBackend. kwargs: model, system_append, mcp_servers,
# max_tool_iterations, cwd, resume.
BackendFactory = Callable[..., object]


class SessionBusy(Exception):
    """A second turn arrived while one is active (contract: 409)."""


class SwitchRefused(Exception):
    """``switch_model``'s guard said no under the lock: the request that
    asked for the switch is no longer a fresh turn (recheck finding)."""


class ModelConflict(Exception):
    """A request named a model other than the one this session is
    pinned to (plan §T2). ``get_or_create`` raises it so the pin is
    never flipped as a side effect of a turn; the API answers it with
    ``SessionPool.switch_model`` (Suti 2026-09-08: the model must be
    changeable mid-chat, like /model in Claude Code)."""

    def __init__(self, pinned: str, requested: str):
        super().__init__(f"session is pinned to {pinned}, not {requested}")
        self.pinned = pinned
        self.requested = requested


def valid_session_id(session_id: str) -> bool:
    return bool(_SESSION_ID_RE.match(session_id))


HANDOVER_TURNS = 40
HANDOVER_CHARS = 12_000


def handover_text(session_dir: Path) -> str | None:
    """The conversation so far, from transcript.jsonl, as a context
    block for a backend that cannot resume the other one's session.
    The newest turns, bounded; None when there is nothing to carry."""
    path = session_dir / "transcript.jsonl"
    if not path.exists():
        return None
    lines: list[str] = []
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            role = rec.get("role")
            content = rec.get("content")
            if role not in ("user", "assistant") or not isinstance(content, str):
                continue
            lines.append(f"{'Suti' if role == 'user' else 'Narada'}: {content.strip()}")
    except OSError:
        return None
    if not lines:
        return None
    tail = lines[-HANDOVER_TURNS:]
    body = "\n".join(tail)
    if len(body) > HANDOVER_CHARS:
        body = body[-HANDOVER_CHARS:]
    return ("[Context — this conversation so far, carried over from another "
            "model. Continue it; do not repeat it back.]\n" + body)


@dataclass
class TurnOutcome:
    text: str
    error: str | None = None  # bounds/backend failure — explicit, never dressed


class BrainSession:
    def __init__(self, session_id: str, tier: str, backend, store: TurnStore,
                 session_dir: Path, model: str = "",
                 brief_stamp: fitness.Stamp | None = None):
        self.session_id = session_id
        self.tier = tier
        self.model = model
        self.backend = backend
        # The akhada briefing this session opened with, as (as_of,
        # rules_rev); a turn that finds it stale prepends a fresh one
        # (plan-exercise-corpus §C2: midnight, a rule or day kind
        # changed from any surface).
        self.brief_stamp = brief_stamp
        self.turns = store
        self.session_dir = session_dir
        self.last_used = time.monotonic()
        self._turn_lock = asyncio.Lock()
        self._reserved = False
        self._cancelled = False

    @property
    def busy(self) -> bool:
        return self._reserved or self._turn_lock.locked()

    def reserve(self) -> None:
        """Synchronously claim the session for one turn — atomic within
        the event loop, so the busy-check and the claim can't be split
        by an await (the double-execution race the diff review caught).
        The executing task releases it via ``release()``."""
        if self.busy:
            raise SessionBusy(self.session_id)
        self._reserved = True

    def release(self) -> None:
        self._reserved = False

    async def run_turn(
        self, prompt, *, deadline_s: float,  # str | list[content-block]
        on_start: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncIterator[object]:
        """Yield assistant text chunks (str) and CardEvents. Caller must hold the reservation
        (``reserve()``); the lock is the in-flight backstop.

        The wall-clock bound ends the turn with an EXPLICIT error (the
        zombie-heartbeat rule): the caller sees a raised TimeoutError,
        never a silent truncation dressed as a completion.
        """
        async with self._turn_lock:
            self._cancelled = False
            self.last_used = time.monotonic()
            if on_start is not None:
                await on_start()
            block, self.brief_stamp = await asyncio.to_thread(
                fitness.refresh_block, self.brief_stamp, "chat")
            if block:
                prompt = fitness.with_preamble(prompt, block)
            started = time.monotonic()
            agen = self.backend.run_turn(prompt)
            completed = False
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
                completed = True
            finally:
                await agen.aclose()
                self.last_used = time.monotonic()
                self._persist_native_id()
                if completed:
                    # The handover, if any, is in the native conversation now.
                    (self.session_dir / "pending-handover.md").unlink(missing_ok=True)

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
    def __init__(self, config: BrainConfig, backend_factory: BackendFactory,
                 backends: dict[str, BackendFactory] | None = None):
        self._config = config
        self._factory = backend_factory
        # Per-backend factories (registry, 2026-09-09); a backend with no
        # entry falls back to the default one (tests: one fake for all).
        self._backends = dict(backends or {})
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

    async def get_or_create(self, session_id: str, tier: str,
                            model: str | None = None) -> BrainSession:
        """`session_id` is the client-chosen name; the pool key and the
        on-disk location are both namespaced by the authenticated tier
        (subdirectory, not `tier:` prefix — `:` is invalid in Windows
        dirnames), so one credential can never open another's session.

        `model` (plan §T2, durable pin): honoured on the session's FIRST
        creation and persisted beside native-session-id; every later
        creation — warm reuse, post-reap recreation, restart — reads the
        pin. A request naming a different model raises ModelConflict
        before any turn is accepted. Sessions from before pinning adopt
        the configured default on first touch."""
        key = f"{tier}:{session_id}"
        async with self._create_lock:
            existing = self._sessions.get(key)
            if existing is not None:
                if model is not None and model != existing.model:
                    raise ModelConflict(existing.model, model)
                return existing
            return await self._create_locked(session_id, tier, model)

    async def _create_locked(self, session_id: str, tier: str,
                             model: str | None) -> BrainSession:
        """Create, start and register a session. The caller holds
        ``_create_lock`` and has ruled out a warm session under ``key``."""
        key = f"{tier}:{session_id}"
        session_dir = self._config.sessions_root / tier / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        resume = None
        native_file = session_dir / "native-session-id"
        if native_file.exists():
            resume = native_file.read_text(encoding="utf-8").strip() or None

        model_file = session_dir / "model"
        pinned = (model_file.read_text(encoding="utf-8").strip() or None
                  if model_file.exists() else None)
        if pinned is not None and model is not None and model != pinned:
            raise ModelConflict(pinned, model)
        resolved = pinned or model or self._config.model
        if pinned is None:
            model_file.write_text(resolved, encoding="utf-8")

        # A native session id belongs to ONE backend: a Claude session
        # id means nothing to Codex and vice versa. Crossing backends
        # (a model switch, or a restart after one) drops the resume and
        # hands the conversation over as text from the brain's own
        # transcript (registry, 2026-09-09).
        backend_name = backend_for(resolved)
        backend_file = session_dir / "backend"
        prior = (backend_file.read_text(encoding="utf-8").strip() or None
                 if backend_file.exists() else None)
        if prior is None and resume is not None:
            # A session from before backends were recorded: its native
            # id is Claude's (review: a cold switch handed it to Codex).
            prior = "claude"
        pending = session_dir / "pending-handover.md"
        handover = None
        if prior is not None and prior != backend_name:
            resume = None
            handover = handover_text(session_dir)
            # The other backend's id must not linger: a later same-backend
            # recreation would resume a foreign session (test caught it).
            native_file.write_text("", encoding="utf-8")
            # Persisted until a turn on the new backend completes (review:
            # a restart before that first turn lost it).
            if handover:
                pending.write_text(handover, encoding="utf-8")
            else:
                pending.unlink(missing_ok=True)
            logger.info("session %s crosses %s → %s: handover of %d chars",
                        key, prior, backend_name, len(handover or ""))
        elif pending.exists():
            handover = pending.read_text(encoding="utf-8") or None
        if prior != backend_name:
            backend_file.write_text(backend_name, encoding="utf-8")

        factory = self._backends.get(backend_name, self._factory)
        # Akhada's briefing rides the system prompt from the first turn
        # (plan-exercise-corpus §C2); computed by code, capped, stamped.
        brief_block, brief_stamp = await asyncio.to_thread(fitness.briefing_block, "chat")
        kwargs = dict(
            model=resolved,
            system_append=self._config.system_append() + brief_block,
            mcp_servers=self._tier_mcp(tier),
            max_tool_iterations=self._config.max_tool_iterations,
            cwd=str(session_dir),
            resume=resume,
            disallowed_tools=self._config.disallowed_tools,
            handover=handover,
        )
        backend = factory(**kwargs)
        await backend.start()
        backend = await self._with_tool_servers(backend, factory, kwargs, key)
        store = TurnStore(
            session_dir / "turns.json", window=self._config.idempotency_window
        )
        session = BrainSession(key, tier, backend, store, session_dir,
                               model=resolved, brief_stamp=brief_stamp)
        self._sessions[key] = session
        logger.info(
            "session %s created (tier=%s, model=%s, resume=%s)",
            key, tier, resolved, bool(resume),
        )
        return session

    async def _with_tool_servers(self, backend, factory, kwargs: dict, key: str):
        """A tool server that died at spawn gets ONE fresh spawn of the
        whole backend before the session is handed over; if it dies
        again, the feed and the log say which and why (Suti 2026-09-16:
        "why didn't we know it was down already and spin it back up?").
        Backends without a status check (fakes, Codex) pass through."""
        check = getattr(backend, "failed_mcp_servers", None)
        if check is None:
            return backend
        failed = await check()
        if not failed:
            return backend
        logger.warning("session %s: tool servers failed at spawn (%s) — respawning once",
                       key, ", ".join(f"{n}: {why}" for n, why in failed.items()))
        try:
            await backend.close()
        except Exception:  # noqa: BLE001
            logger.warning("session %s: close of the failed backend raised", key, exc_info=True)
        backend = factory(**kwargs)
        await backend.start()
        failed = await backend.failed_mcp_servers()
        if failed:
            from prana.brain import mcp_probe

            await asyncio.to_thread(mcp_probe.report, failed, f"session {key}")
        else:
            logger.info("session %s: tool servers came up on the second spawn", key)
        return backend

    def configured_mcp(self) -> dict:
        """Every tool server the brain can wire (the prana tier's set)."""
        return dict(self._tier_mcp("prana"))

    async def switch_model(self, session_id: str, tier: str, model: str,
                           guard: Callable[[BrainSession | None], bool] | None = None,
                           ) -> BrainSession:
        """Re-pin a session to ``model`` mid-life. The warm client is
        closed and a new one starts on the SAME native transcript
        (resume), so the conversation continues under the new model —
        /model, not a new chat. Refused (SessionBusy) while a turn is
        in flight: the running turn's model must not change under it.

        One locked operation end to end (review): competing switches
        serialize and the last one wins cleanly, never a ModelConflict
        escaping from a re-creation that lost the race for the pin.

        ``guard`` runs UNDER the lock, before anything changes, with the
        warm session (or None): the caller's "is this still a fresh
        turn?" check. Waiting for the lock is an await, and a queued
        request can complete the same request_id meanwhile; a False
        guard raises SwitchRefused and nothing moves (recheck)."""
        key = f"{tier}:{session_id}"
        async with self._create_lock:
            existing = self._sessions.get(key)
            if guard is not None and not guard(existing):
                raise SwitchRefused(key)
            if existing is not None:
                if existing.model == model:
                    return existing
                if existing.busy:
                    raise SessionBusy(key)
                # Unregister BEFORE awaiting close: the reaper must not
                # find (and double-close) a half-switched session (review).
                self._sessions.pop(key, None)
                existing._persist_native_id()
                await existing.backend.close()
            session_dir = self._config.sessions_root / tier / session_id
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "model").write_text(model, encoding="utf-8")
            logger.info("session %s re-pinned to %s", key, model)
            return await self._create_locked(session_id, tier, model)

    def peek(self, session_id: str, tier: str) -> BrainSession | None:
        return self._sessions.get(f"{tier}:{session_id}")

    def start_reaper(self) -> None:
        self._reaper_task = asyncio.get_running_loop().create_task(self._reap())

    async def _reap(self) -> None:
        while True:
            await asyncio.sleep(60)
            await self.reap_once()

    async def reap_once(self) -> None:
        cutoff = time.monotonic() - self._config.idle_ttl_s
        for sid, sess in list(self._sessions.items()):
            if sess.last_used < cutoff and not sess.busy:
                # Identity re-check: a switch may have replaced this
                # entry while an earlier close awaited (review).
                if self._sessions.get(sid) is not sess:
                    continue
                logger.info("reaping idle session %s", sid)
                self._sessions.pop(sid, None)
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
