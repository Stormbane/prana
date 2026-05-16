"""Event log — the spine of the bus.

Events live in state.db (the same SQLite database utterance_queue lives in).
WAL + monotonic id + JSON payload. Publishers append; subscribers tail with
``WHERE id > since``.

Schema is additive — reserved fields (scope, requires_role, budget_hint,
trace_id) are persisted when set, NULL otherwise. No consumer enforces them
yet; they're slots for future privacy / auth / cost work.

Canonical event:

    {
      "id":             1234,
      "ts":             "2026-05-11T14:03:27.123Z",
      "kind":           "sense" | "sense_edge" | "action_invoke" |
                        "action_result" | "skill_invoke" | "skill_result" |
                        "cognition_trigger" | "cognition_result",
      "name":           "presence" | "speak" | "research-and-report" | …,
      "source":         "deha-body" | "heartbeat-cron" | "chat-bridge" | …,
      "payload":        {...},
      "scope":          "public" | "private" | "sensitive" | None,
      "requires_role":  "owner" | "trusted" | "guest" | None,
      "budget_hint":    {"usd_max": 0.50, "time_max_s": 60} | None,
      "trace_id":       "<uuid>" | None
    }
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterator, Optional

from prana.state.db import get_db, init_db, NARADA_STATE_DB

logger = logging.getLogger(__name__)


class EventKind(str, Enum):
    SENSE = "sense"
    SENSE_EDGE = "sense_edge"
    ACTION_INVOKE = "action_invoke"
    ACTION_RESULT = "action_result"
    SKILL_INVOKE = "skill_invoke"
    SKILL_RESULT = "skill_result"
    COGNITION_TRIGGER = "cognition_trigger"
    COGNITION_RESULT = "cognition_result"


_BUS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL,                     -- ISO8601 UTC
    kind            TEXT    NOT NULL,                     -- EventKind value
    name            TEXT    NOT NULL,                     -- specific sense/action/etc
    source          TEXT    NOT NULL,                     -- who published
    payload         TEXT    NOT NULL DEFAULT '{}',        -- JSON
    -- Reserved fields (no consumer enforces them yet)
    scope           TEXT,                                 -- public|private|sensitive
    requires_role   TEXT,                                 -- owner|trusted|guest
    budget_hint     TEXT,                                 -- JSON: {usd_max, time_max_s}
    trace_id        TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_kind_name_id
    ON events(kind, name, id);

CREATE INDEX IF NOT EXISTS idx_events_id_kind
    ON events(id, kind);
"""


@dataclass
class Event:
    """In-memory event view. Mirrors the events table row 1:1."""
    id: int
    ts: str
    kind: str
    name: str
    source: str
    payload: dict[str, Any] = field(default_factory=dict)
    scope: Optional[str] = None
    requires_role: Optional[str] = None
    budget_hint: Optional[dict[str, Any]] = None
    trace_id: Optional[str] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Event":
        def _json_or_none(v: Optional[str]) -> Optional[dict]:
            if not v:
                return None
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                logger.warning("bus event id=%s has unparseable JSON in budget_hint", row["id"])
                return None

        return cls(
            id=row["id"],
            ts=row["ts"],
            kind=row["kind"],
            name=row["name"],
            source=row["source"],
            payload=json.loads(row["payload"]) if row["payload"] else {},
            scope=row["scope"],
            requires_role=row["requires_role"],
            budget_hint=_json_or_none(row["budget_hint"]),
            trace_id=row["trace_id"],
        )


def init_bus() -> None:
    """Create the events table if missing. Idempotent."""
    init_db()
    conn = get_db()
    try:
        conn.executescript(_BUS_SCHEMA_SQL)
        logger.debug("bus events table initialized in %s", NARADA_STATE_DB)
    finally:
        conn.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def publish(
    kind: EventKind | str,
    name: str,
    payload: dict[str, Any] | None = None,
    *,
    source: str,
    scope: Optional[str] = None,
    requires_role: Optional[str] = None,
    budget_hint: Optional[dict[str, Any]] = None,
    trace_id: Optional[str] = None,
) -> int:
    """Append an event. Returns its row id.

    `kind` may be the EventKind enum or its string value — both work.
    `name` is the specific sense/action/skill — e.g. 'presence', 'speak',
    'research-and-report'. `source` is who's publishing (process name).
    Reserved fields are optional; pass them when meaningful.
    """
    if isinstance(kind, EventKind):
        kind_str = kind.value
    else:
        kind_str = str(kind)
        # Validate against the enum — typos here are very expensive
        if kind_str not in {k.value for k in EventKind}:
            raise ValueError(f"unknown event kind: {kind_str!r}")

    payload_json = json.dumps(payload or {}, default=str)
    budget_json = json.dumps(budget_hint) if budget_hint else None

    init_bus()
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO events "
            "(ts, kind, name, source, payload, scope, requires_role, budget_hint, trace_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _now_iso(),
                kind_str,
                name,
                source,
                payload_json,
                scope,
                requires_role,
                budget_json,
                trace_id,
            ),
        )
        eid = cur.lastrowid
        logger.debug("bus publish #%d %s:%s (source=%s)", eid, kind_str, name, source)
        return eid
    finally:
        conn.close()


def tail(
    since_id: int = 0,
    *,
    kinds: Optional[list[str]] = None,
    names: Optional[list[str]] = None,
    limit: int = 100,
) -> list[Event]:
    """Return events with id > since_id, filtered by kind/name.

    Non-blocking — returns immediately with whatever's in the table.
    Subscribers can poll this in a loop (the simplest pattern) or use
    `subscribe()` for a generator that long-polls.
    """
    init_bus()
    sql = ["SELECT * FROM events WHERE id > ?"]
    params: list[Any] = [since_id]

    if kinds:
        placeholders = ",".join("?" for _ in kinds)
        sql.append(f"AND kind IN ({placeholders})")
        params.extend(kinds)

    if names:
        placeholders = ",".join("?" for _ in names)
        sql.append(f"AND name IN ({placeholders})")
        params.extend(names)

    sql.append("ORDER BY id ASC LIMIT ?")
    params.append(limit)

    conn = get_db()
    try:
        rows = conn.execute(" ".join(sql), params).fetchall()
        return [Event.from_row(r) for r in rows]
    finally:
        conn.close()


def latest_sense(name: str) -> Optional[Event]:
    """Most recent published reading of a named sense — for pull access."""
    init_bus()
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM events "
            "WHERE kind IN (?, ?) AND name = ? "
            "ORDER BY id DESC LIMIT 1",
            (EventKind.SENSE.value, EventKind.SENSE_EDGE.value, name),
        ).fetchone()
        return Event.from_row(row) if row else None
    finally:
        conn.close()


def subscribe(
    *,
    kinds: Optional[list[str]] = None,
    names: Optional[list[str]] = None,
    since_id: int = 0,
    poll_interval_s: float = 0.5,
) -> Iterator[Event]:
    """Generator that yields events as they arrive.

    Polls the events table every poll_interval_s. Caller should consume
    in a loop and break when done. This is the simplest cross-process
    subscription primitive — fine for our scale (one user, dozens of
    events/min worst case).

    For higher-fidelity push (cross-host, lower latency), the HTTP
    gateway adds /events?since=<id> as SSE.
    """
    last = since_id
    while True:
        batch = tail(since_id=last, kinds=kinds, names=names, limit=200)
        for event in batch:
            yield event
            last = event.id
        if not batch:
            time.sleep(poll_interval_s)
