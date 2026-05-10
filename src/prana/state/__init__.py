"""prana.state — cross-process state for the antahkarana.

The third-layer state model from the project decomposition plan:

  Hot:   ~/.narada/state.db (this module)  — utterance queue, current
                                              cycle slice, recent events
  Warm:  ~/.narada/heartbeat/cycles/        — recent cycle records
  Cold:  smriti memory tree                 — long-term memory

state.db is the SQLite-WAL coordination layer between heartbeat
(producer of utterances) and the body / telegram bridge (consumers).
Scoped strictly to what Hermes does NOT cover — Hermes already owns
session/conversation state in ~/.hermes/state.db, do not shadow it.
"""

from prana.state.db import get_db, init_db, NARADA_STATE_DB
from prana.state.utterance_queue import (
    Utterance,
    push_utterance,
    mark_delivered,
    mark_skipped,
    pending_utterances,
)
from prana.state.proximity import is_at_pc, idle_seconds
from prana.state.presence import (
    is_present,
    body_sees_someone,
    presence_snapshot,
)
from prana.state.router import route_utterance, RouteResult

__all__ = [
    "get_db",
    "init_db",
    "NARADA_STATE_DB",
    "Utterance",
    "push_utterance",
    "mark_delivered",
    "mark_skipped",
    "pending_utterances",
    "is_at_pc",
    "idle_seconds",
    "is_present",
    "body_sees_someone",
    "presence_snapshot",
    "route_utterance",
    "RouteResult",
]
