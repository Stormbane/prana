"""Session registry — the lifecycle state machine over sessions.db.

States and legal transitions:

    spawning ─→ running ─→ idle ─→ running   (activity resumes)
        │           │        │
        │           ├────────┴──→ done       (clean exit)
        │           ├──→ hung                (no activity past hard timeout)
        │           └──→ killed              (explicit cancel)
        └──→ failed                          (never came up)

    any live state ──reconcile()──→ dead     (process vanished outside us)

The registry never touches processes itself except through reconcile();
adapters own spawning and the JobObject. What the registry guarantees is
that sessions.db never claims a session is live when its process is not.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Optional

from prana.sessions.db import SESSIONS_DB, get_db, init_db

logger = logging.getLogger(__name__)


class SessionState(str, Enum):
    SPAWNING = "spawning"
    RUNNING = "running"
    IDLE = "idle"
    DONE = "done"
    HUNG = "hung"
    KILLED = "killed"
    FAILED = "failed"
    DEAD = "dead"  # reconciliation found the process gone

    @property
    def live(self) -> bool:
        return self in (SessionState.SPAWNING, SessionState.RUNNING, SessionState.IDLE)


_LEGAL: dict[SessionState, frozenset[SessionState]] = {
    SessionState.SPAWNING: frozenset(
        {SessionState.RUNNING, SessionState.FAILED, SessionState.KILLED, SessionState.DEAD}
    ),
    SessionState.RUNNING: frozenset(
        {SessionState.IDLE, SessionState.DONE, SessionState.HUNG,
         SessionState.KILLED, SessionState.DEAD}
    ),
    SessionState.IDLE: frozenset(
        {SessionState.RUNNING, SessionState.DONE, SessionState.HUNG,
         SessionState.KILLED, SessionState.DEAD}
    ),
    SessionState.HUNG: frozenset({SessionState.KILLED, SessionState.DEAD}),
    SessionState.DONE: frozenset(),
    SessionState.KILLED: frozenset(),
    SessionState.FAILED: frozenset(),
    SessionState.DEAD: frozenset(),
}


class IllegalTransition(RuntimeError):
    pass


@dataclass
class Session:
    id: str
    provider: str
    cwd: str
    state: SessionState
    title: str = ""
    provider_session_id: Optional[str] = None
    pid: Optional[int] = None
    pid_created_at: Optional[float] = None
    pane_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""
    last_activity_at: Optional[str] = None
    ended_at: Optional[str] = None
    exit_code: Optional[int] = None
    last_error: Optional[str] = None

    @classmethod
    def _from_row(cls, row: sqlite3.Row) -> "Session":
        d = dict(row)
        d["state"] = SessionState(d["state"])
        return cls(**d)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionRegistry:
    """All reads/writes to the sessions table go through here."""

    def __init__(self, db_path: Path = SESSIONS_DB) -> None:
        self._db_path = db_path
        init_db(db_path)

    def _conn(self) -> sqlite3.Connection:
        return get_db(self._db_path)

    # ── create / read ────────────────────────────────────────────────

    def create(
        self,
        provider: str,
        cwd: str,
        *,
        title: str = "",
        idempotency_key: Optional[str] = None,
    ) -> Session:
        """Insert a new session in SPAWNING state.

        If idempotency_key matches an existing session that is live or
        completed, return that session instead of creating a duplicate —
        a retried voice command must not double-spawn.
        """
        conn = self._conn()
        try:
            if idempotency_key:
                row = conn.execute(
                    "SELECT * FROM sessions WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if row is not None:
                    existing = Session._from_row(row)
                    logger.info(
                        "create: idempotency hit %s -> existing %s (%s)",
                        idempotency_key, existing.id, existing.state.value,
                    )
                    return existing
            sid = str(uuid.uuid4())
            now = _now()
            conn.execute(
                """INSERT INTO sessions
                   (id, provider, cwd, title, state, idempotency_key,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (sid, provider, cwd, title, SessionState.SPAWNING.value,
                 idempotency_key, now, now),
            )
            return self.get(sid)
        finally:
            conn.close()

    def find_by_idempotency_key(self, key: str) -> Optional[Session]:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM sessions WHERE idempotency_key = ?", (key,)
            ).fetchone()
            return Session._from_row(row) if row is not None else None
        finally:
            conn.close()

    def get(self, session_id: str) -> Session:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"no session {session_id}")
            return Session._from_row(row)
        finally:
            conn.close()

    def list(self, *, live_only: bool = False) -> list[Session]:
        conn = self._conn()
        try:
            if live_only:
                rows = conn.execute(
                    "SELECT * FROM sessions WHERE state IN (?, ?, ?)"
                    " ORDER BY created_at",
                    tuple(s.value for s in SessionState if s.live),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM sessions ORDER BY created_at"
                ).fetchall()
            return [Session._from_row(r) for r in rows]
        finally:
            conn.close()

    # ── transitions ──────────────────────────────────────────────────

    def transition(
        self,
        session_id: str,
        to: SessionState,
        *,
        pid: Optional[int] = None,
        pid_created_at: Optional[float] = None,
        provider_session_id: Optional[str] = None,
        pane_id: Optional[str] = None,
        exit_code: Optional[int] = None,
        error: Optional[str] = None,
    ) -> Session:
        """Move a session to a new state, enforcing legality."""
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise KeyError(f"no session {session_id}")
            current = SessionState(row["state"])
            if to not in _LEGAL[current]:
                conn.execute("ROLLBACK")
                raise IllegalTransition(
                    f"{session_id}: {current.value} -> {to.value} not allowed"
                )
            now = _now()
            sets = ["state = ?", "updated_at = ?"]
            args: list = [to.value, now]
            if pid is not None:
                sets.append("pid = ?"); args.append(pid)
            if pid_created_at is not None:
                sets.append("pid_created_at = ?"); args.append(pid_created_at)
            if provider_session_id is not None:
                sets.append("provider_session_id = ?"); args.append(provider_session_id)
            if pane_id is not None:
                sets.append("pane_id = ?"); args.append(pane_id)
            if exit_code is not None:
                sets.append("exit_code = ?"); args.append(exit_code)
            if error is not None:
                sets.append("last_error = ?"); args.append(error)
            if not to.live:
                sets.append("ended_at = ?"); args.append(now)
            args.append(session_id)
            conn.execute(
                f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", args
            )
            conn.execute("COMMIT")
            logger.info("session %s: %s -> %s", session_id, current.value, to.value)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            conn.close()
        return self.get(session_id)

    def touch(self, session_id: str) -> None:
        """Record activity (moves IDLE back to RUNNING implicitly)."""
        conn = self._conn()
        try:
            now = _now()
            conn.execute(
                "UPDATE sessions SET last_activity_at = ?, updated_at = ?,"
                " state = CASE WHEN state = 'idle' THEN 'running' ELSE state END"
                " WHERE id = ?",
                (now, now, session_id),
            )
        finally:
            conn.close()

    # ── reconciliation ───────────────────────────────────────────────

    def reconcile(
        self,
        session_alive: Callable[[Session], bool],
        live_pane_ids: Optional[Iterable[str]] = None,
    ) -> list[Session]:
        """Mark live-in-db sessions DEAD when their process is gone.

        Called on manager startup and periodically. ``session_alive``
        is injected (the manager's pid+create_time identity check in
        production — bare pid existence is NOT enough, pids get reused)
        so tests control it. A closed wezterm pane alone does not kill
        a session (the CLI may be headless); it clears the stale pane
        mapping.
        """
        panes = set(live_pane_ids) if live_pane_ids is not None else None
        marked: list[Session] = []
        for sess in self.list(live_only=True):
            if not session_alive(sess):
                marked.append(
                    self.transition(
                        sess.id, SessionState.DEAD,
                        error="reconcile: process not found",
                    )
                )
                continue
            if panes is not None and sess.pane_id and sess.pane_id not in panes:
                conn = self._conn()
                try:
                    conn.execute(
                        "UPDATE sessions SET pane_id = NULL, updated_at = ?"
                        " WHERE id = ?",
                        (_now(), sess.id),
                    )
                finally:
                    conn.close()
                logger.info(
                    "session %s: pane %s closed, mapping cleared",
                    sess.id, sess.pane_id,
                )
        if marked:
            logger.warning("reconcile: marked %d session(s) dead", len(marked))
        return marked

    def sweep_timeouts(
        self, *, idle_after_s: float, hung_after_s: float
    ) -> list[Session]:
        """RUNNING with no activity → IDLE; IDLE far past activity → HUNG."""
        changed: list[Session] = []
        now = datetime.now(timezone.utc)
        for sess in self.list(live_only=True):
            ref = sess.last_activity_at or sess.updated_at or sess.created_at
            try:
                last = datetime.fromisoformat(ref)
            except ValueError:
                continue
            age = (now - last).total_seconds()
            if sess.state is SessionState.RUNNING and age >= idle_after_s:
                changed.append(self.transition(sess.id, SessionState.IDLE))
            elif sess.state is SessionState.IDLE and age >= hung_after_s:
                changed.append(
                    self.transition(
                        sess.id, SessionState.HUNG,
                        error=f"no activity for {int(age)}s",
                    )
                )
        return changed
