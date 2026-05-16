"""prana.bus — event bus for the unified mind.

Architecture in docs/plans/unified-mind-2026-05-11.md.

Three categories of bus citizen (see plan):
  - senses   — passive observations, pulled or subscribed
  - actions  — invoked side-effects, one entry one outcome
  - skills   — named workflows composing senses + actions + reasoning

Local transport: state.db events table (WAL, monotonic id, tail with
WHERE id > since). Cross-host transport (Phase 1B+): HTTP gateway on
127.0.0.1:8770.

Reserved event fields named now to prevent breaking changes later:
scope, requires_role, budget_hint, trace_id. See bus.events for the
canonical event shape.
"""

from prana.bus.events import (
    Event,
    publish,
    tail,
    latest_sense,
    init_bus,
    EventKind,
)

__all__ = [
    "Event",
    "EventKind",
    "publish",
    "tail",
    "latest_sense",
    "init_bus",
]
