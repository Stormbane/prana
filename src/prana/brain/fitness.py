"""Akhada's briefing, put in front of the model by default
(plan-exercise-corpus-2026-09-18 §C2; Suti: "Tuesdays I fast, I've
told him, but it isn't highlighted on a Tuesday").

Two doors, both surfaces:

- ``briefing_block(surface)`` — the block a session opens with, and
  the stamp ``(as_of, rules_rev)`` it was built from.
- ``refresh_block(stamp, surface)`` — a replacement when the stamp has
  gone stale: midnight rolled, or a rule / day kind changed from any
  surface. The chat brain prepends it to the next turn's prompt; the
  voice workers rewrite the agent's instructions.

Akhada is imported lazily and every failure yields nothing: a session
without a briefing is a session, a failed session is not. Tests
inject a provider through ``set_provider``.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

Stamp = tuple[str, int]

HEADERS = {
    "chat": "AKHADA (today, computed from his log — act on it, don't re-fetch):",
    "voice": "FITNESS (Akhada):",
}
REFRESH_HEADERS = {
    "chat": "TODAY (updated since this session opened):",
    "voice": "FITNESS (Akhada):",
}

_provider: tuple[Callable[..., Any], Callable[..., bool]] | None = None


def set_provider(briefing: Callable[..., Any] | None, is_current: Callable[..., bool] | None) -> None:
    """Test seam: ``briefing(surface=...)`` returning an object with
    ``text``, ``as_of``, ``rules_rev``; ``is_current(store, as_of, rev)``."""
    global _provider
    _provider = (briefing, is_current) if briefing and is_current else None


def _load() -> tuple[Callable[..., Any], Callable[..., bool]] | None:
    if _provider is not None:
        return _provider
    try:
        from akhada.context import briefing, is_current
    except ImportError:
        return None
    return briefing, is_current


def briefing_block(surface: str = "chat") -> tuple[str, Stamp | None]:
    """The opening block (with a leading blank line) and its stamp;
    ("", None) when akhada is absent or broken."""
    mods = _load()
    if mods is None:
        return "", None
    try:
        b = mods[0](surface=surface)
    except Exception as exc:  # noqa: BLE001 — never a failed session
        logger.warning("akhada briefing failed: %s", exc)
        return "", None
    return f"\n\n{HEADERS.get(surface, HEADERS['chat'])}\n{b.text}", (str(b.as_of), int(b.rules_rev))


def refresh_block(stamp: Stamp | None, surface: str = "chat") -> tuple[str | None, Stamp | None]:
    """(block, new stamp) when the stamp is stale; (None, stamp) when it
    still describes today and the rules. Never raises."""
    if stamp is None:
        return None, None
    mods = _load()
    if mods is None:
        return None, stamp
    try:
        if mods[1](None, stamp[0], stamp[1]):
            return None, stamp
        b = mods[0](surface=surface)
    except Exception as exc:  # noqa: BLE001
        logger.warning("akhada briefing refresh failed: %s", exc)
        return None, stamp
    header = REFRESH_HEADERS.get(surface, REFRESH_HEADERS["chat"])
    return f"{header}\n{b.text}", (str(b.as_of), int(b.rules_rev))


def with_preamble(prompt: Any, block: str) -> Any:
    """Prepend a block to a turn's prompt — a string, or a list of
    content blocks (the multimodal shape)."""
    if isinstance(prompt, str):
        return block + "\n\n" + prompt
    if isinstance(prompt, list):
        return [{"type": "text", "text": block}, *prompt]
    return prompt
