"""Akhada's briefing, put in front of the model by default
(plan-exercise-corpus-2026-09-18 §C2; Suti: "Tuesdays I fast, I've
told him, but it isn't highlighted on a Tuesday").

Two doors, both surfaces:

- ``briefing_block(surface)`` — the block a session opens with, and
  the stamp ``(as_of, rules_rev)`` it was built from.
- ``refresh_block(stamp, surface)`` — a replacement when the stamp has
  gone stale: midnight rolled, or a rule / day kind changed from any
  surface. A missing stamp (the opening failed) is treated as "needs
  its first briefing", so one bad open never silences a session
  (diff review). The chat brain prepends the block to the next turn's
  prompt; the voice workers rewrite the agent's instructions. Callers
  advance their stamp only once the block was DELIVERED.

Everything akhada-related — import, call, the shape of the result —
sits inside one guard, and every failure yields nothing: a session
without a briefing is a session, a failed session is not. Tests inject
a provider through ``set_provider``.
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
    from akhada.context import briefing, is_current  # may raise: caught by the callers

    return briefing, is_current


def _fetch(surface: str) -> tuple[str, Stamp] | None:
    """One guarded trip: load, call, validate. None on any failure."""
    try:
        mods = _load()
        if mods is None:
            return None
        b = mods[0](surface=surface)
        text = str(b.text)
        stamp = (str(b.as_of), int(b.rules_rev))
        if not text.strip() or not stamp[0]:
            raise ValueError("empty briefing")
        return text, stamp
    except Exception as exc:  # noqa: BLE001 — never a failed session
        logger.warning("akhada briefing unavailable: %s", exc)
        return None


def _current(stamp: Stamp) -> bool:
    """Whether the stamp still describes today and the rules. A failed
    check reads as current: better a stale line than a dead turn."""
    try:
        mods = _load()
        return True if mods is None else bool(mods[1](None, stamp[0], stamp[1]))
    except Exception as exc:  # noqa: BLE001
        logger.warning("akhada briefing check failed: %s", exc)
        return True


def briefing_block(surface: str = "chat") -> tuple[str, Stamp | None]:
    """The opening block (with a leading blank line) and its stamp;
    ("", None) when akhada is absent or broken."""
    got = _fetch(surface)
    if got is None:
        return "", None
    text, stamp = got
    return f"\n\n{HEADERS.get(surface, HEADERS['chat'])}\n{text}", stamp


def refresh_block(stamp: Stamp | None, surface: str = "chat") -> tuple[str | None, Stamp | None]:
    """(block, new stamp) when a briefing is due — the stamp is stale,
    or there never was one; (None, stamp) when it still holds. Never
    raises. The caller keeps its old stamp until the block is delivered."""
    if stamp is not None and _current(stamp):
        return None, stamp
    got = _fetch(surface)
    if got is None:
        return None, stamp
    text, new_stamp = got
    header = (REFRESH_HEADERS if stamp is not None else HEADERS).get(surface, HEADERS["chat"])
    return f"{header}\n{text}", new_stamp


def with_preamble(prompt: Any, block: str) -> Any:
    """Prepend a block to a turn's prompt — a string, or a list of
    content blocks (the multimodal shape)."""
    if isinstance(prompt, str):
        return block + "\n\n" + prompt
    if isinstance(prompt, list):
        return [{"type": "text", "text": block}, *prompt]
    return prompt
