"""prana.bus.actions — action handlers.

An action is a named side-effect invoked via the bus. The pattern:

  1. Publisher (e.g. cognition / heartbeat) publishes ACTION_INVOKE.
  2. Handler subscribes, executes the side-effect, publishes
     ACTION_RESULT with the outcome.
  3. Auditable: every action leaves invoke + result rows in events.

Today's handlers:
  - speak — wraps prana.state.router.route_utterance (body or telegram).

Each handler is independently importable so it can be used either:
  - Inline (`speak.invoke(text=…)` — synchronous, calls back through bus
    for the audit but executes immediately)
  - As a subscriber loop (a separate process that drains the bus —
    not built yet; deferred until a real consumer needs it)
"""

from prana.bus.actions.speak import invoke_speak

__all__ = ["invoke_speak"]
