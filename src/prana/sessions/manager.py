"""SessionManager — ties registry + adapters + panes together.

One instance per process. Owns the live SpawnedProcess handles (the
registry only knows pids); enforces concurrency caps; runs reconcile on
startup and timeout sweeps on demand.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import psutil

from prana.sessions import panes
from prana.sessions.adapters import (
    PROVIDERS,
    SPAWNERS,
    SessionEvent,
    SpawnedProcess,
    send_claude_followup,
)
from prana.sessions.db import SESSIONS_DB
from prana.sessions.registry import Session, SessionRegistry, SessionState

logger = logging.getLogger(__name__)

# Subscriptions are shared with Suti's own interactive use — cap hard.
DEFAULT_GLOBAL_CAP = 6
DEFAULT_PER_PROVIDER_CAP = 3

IDLE_AFTER_S = 120.0        # RUNNING with no events → IDLE
HUNG_AFTER_S = 30 * 60.0    # IDLE that long → HUNG


class CapExceeded(RuntimeError):
    pass


def _proc_create_time(pid: int) -> Optional[float]:
    try:
        return psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def _session_alive(sess: Session) -> bool:
    """Liveness = pid exists AND identity matches (pids get reused).

    Rows from before the identity column (pid_created_at None) fall
    back to bare existence — the best available for legacy rows.
    """
    if sess.pid is None:
        return False
    created = _proc_create_time(sess.pid)
    if created is None:
        return False
    if sess.pid_created_at is None:
        return True
    return abs(created - sess.pid_created_at) < 1.0


@dataclass
class ManagerConfig:
    db_path: Path = SESSIONS_DB
    global_cap: int = DEFAULT_GLOBAL_CAP
    per_provider_cap: int = DEFAULT_PER_PROVIDER_CAP
    mirror_panes: bool = False   # spawn a wezterm tail pane per session
    extra_env: dict = field(default_factory=dict)


class SessionManager:
    def __init__(self, config: Optional[ManagerConfig] = None) -> None:
        self.config = config or ManagerConfig()
        self.registry = SessionRegistry(self.config.db_path)
        self._procs: dict[str, SpawnedProcess] = {}
        self._output: dict[str, deque[str]] = {}
        self._lock = threading.Lock()
        self.reconcile()

    # ── queries ──────────────────────────────────────────────────────

    def list_sessions(self, *, live_only: bool = False) -> list[Session]:
        return self.registry.list(live_only=live_only)

    def get(self, session_id: str) -> Session:
        return self.registry.get(session_id)

    def recent_output(self, session_id: str, limit: int = 50) -> list[str]:
        """Recent normalized output lines for an owned session (this
        process's ring buffer — empty after a manager restart)."""
        buf = self._output.get(session_id)
        if not buf:
            return []
        return list(buf)[-limit:]

    # ── spawn ────────────────────────────────────────────────────────

    def _check_caps(self, provider: str) -> None:
        live = self.registry.list(live_only=True)
        if len(live) >= self.config.global_cap:
            raise CapExceeded(
                f"global cap {self.config.global_cap} reached"
            )
        per = sum(1 for s in live if s.provider == provider)
        if per >= self.config.per_provider_cap:
            raise CapExceeded(
                f"{provider} cap {self.config.per_provider_cap} reached"
            )

    def spawn(
        self,
        provider: str,
        cwd: str,
        prompt: str,
        *,
        title: str = "",
        idempotency_key: Optional[str] = None,
        resume_session_id: Optional[str] = None,
    ) -> Session:
        """Spawn a session. Idempotent on idempotency_key."""
        if provider not in PROVIDERS:
            raise ValueError(f"unknown provider {provider!r}")
        with self._lock:
            if idempotency_key:
                existing = self.registry.find_by_idempotency_key(idempotency_key)
                if existing is not None:
                    return existing
            self._check_caps(provider)
            sess = self.registry.create(
                provider, cwd, title=title or prompt[:60],
                idempotency_key=idempotency_key,
            )

            def on_event(event: SessionEvent, _sid=sess.id) -> None:
                self._handle_event(_sid, event)

            try:
                sp = SPAWNERS[provider](
                    prompt, cwd, on_event,
                    resume_session_id=resume_session_id,
                )
            except Exception as exc:
                self.registry.transition(
                    sess.id, SessionState.FAILED, error=str(exc)
                )
                raise
            self._procs[sess.id] = sp
            result = self.registry.transition(
                sess.id, SessionState.RUNNING, pid=sp.pid,
                pid_created_at=_proc_create_time(sp.pid),
            )
            # Only now may events flow — a fast-exiting process must not
            # race the SPAWNING -> RUNNING transition above.
            sp.start()
            return result

    def _handle_event(self, session_id: str, event: SessionEvent) -> None:
        try:
            if event.kind == "exit":
                code = int(event.raw.get("exit_code") or 0)
                sess = self.registry.get(session_id)
                if sess.state.live:
                    if sess.state is SessionState.SPAWNING:
                        to = SessionState.FAILED
                    elif code == 0:
                        to = SessionState.DONE
                    else:
                        to = SessionState.DEAD  # process gone, not by our hand
                    self.registry.transition(
                        session_id, to, exit_code=code,
                        error=None if code == 0 else f"exit code {code}",
                    )
                with self._lock:
                    self._procs.pop(session_id, None)
                return
            if event.provider_session_id:
                sess = self.registry.get(session_id)
                if sess.provider_session_id != event.provider_session_id:
                    self._set_provider_session_id(
                        session_id, event.provider_session_id
                    )
            if event.text:
                buf = self._output.setdefault(session_id, deque(maxlen=200))
                buf.append(f"[{event.kind}] {event.text}")
            self.registry.touch(session_id)
        except Exception as exc:
            logger.warning("event handling for %s failed: %s", session_id, exc)

    def _set_provider_session_id(self, session_id: str, psid: str) -> None:
        conn = self.registry._conn()
        try:
            conn.execute(
                "UPDATE sessions SET provider_session_id = ? WHERE id = ?",
                (psid, session_id),
            )
        finally:
            conn.close()

    # ── steer / cancel ───────────────────────────────────────────────

    def relay(self, session_id: str, text: str) -> bool:
        """Send a follow-up into a live owned session (claude only, v1)."""
        sess = self.registry.get(session_id)
        if not sess.state.live:
            raise RuntimeError(f"session {session_id} is {sess.state.value}")
        sp = self._procs.get(session_id)
        if sp is None:
            raise RuntimeError(
                f"session {session_id} has no live process in this manager"
            )
        if sess.provider == "claude":
            send_claude_followup(sp, text)
            self.registry.touch(session_id)
            return True
        raise RuntimeError(
            f"relay not supported for provider {sess.provider} yet"
        )

    def cancel(self, session_id: str) -> Session:
        sess = self.registry.get(session_id)
        sp = self._procs.pop(session_id, None)
        if sp is not None:
            sp.kill()
        elif _session_alive(sess):
            # We hold no handle (manager restarted) but pid IDENTITY
            # matches the record — kill by pid, children first, so
            # KILLED never leaves a live tree behind. Without the
            # identity match we refuse: a reused pid means the session
            # is already gone and the pid belongs to an innocent.
            assert sess.pid is not None
            try:
                root = psutil.Process(sess.pid)
                procs = root.children(recursive=True) + [root]
                for p in procs:
                    try:
                        p.kill()
                    except psutil.NoSuchProcess:
                        pass
                psutil.wait_procs(procs, timeout=5)
            except psutil.NoSuchProcess:
                pass
        elif sess.state.live:
            # process already gone (or identity mismatch): honest state
            # is DEAD, not KILLED — we killed nothing.
            return self.registry.transition(
                session_id, SessionState.DEAD,
                error="cancel: process already gone or pid reused",
            )
        if sess.state.live:
            return self.registry.transition(session_id, SessionState.KILLED)
        return sess

    # ── maintenance ──────────────────────────────────────────────────

    def reconcile(self) -> list[Session]:
        pane_ids = panes.list_pane_ids() if panes.available() else None
        return self.registry.reconcile(_session_alive, pane_ids)

    def sweep(self) -> list[Session]:
        changed = self.registry.sweep_timeouts(
            idle_after_s=IDLE_AFTER_S, hung_after_s=HUNG_AFTER_S
        )
        for sess in changed:
            if sess.state is SessionState.HUNG:
                logger.warning("session %s is HUNG: %s", sess.id, sess.title)
        return changed
