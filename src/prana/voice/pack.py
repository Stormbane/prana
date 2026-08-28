"""Voice context packs — a mind that knows Suti, tiered (M2 §2.1, B3).

Two DISJOINT roots, two SEPARATE builders (cross-review round 2 on the
M2 spec):

- ``~/.narada/voice-pack/shareable/`` — what any listener may hear.
  build_shareable() opens THIS directory and nothing else, by
  construction: tool tiers cannot protect data already sitting in the
  model's instructions, so the pack itself is the boundary.
- ``~/.narada/voice-pack/personal/`` — Suti essentials, curated to the
  ratified "wall calendar rule" (nothing a guest standing in the room
  couldn't learn from the walls). Assembled ONLY after verified tap
  admission, on top of the shareable pack, plus open-thread headlines.

Everything here is a deliberate allowlist Suti can audit by reading one
folder; it is sent to the model provider with the session — curation
happens at write time, in these files, not by filtering.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

VOICE_PACK_ROOT = Path.home() / ".narada" / "voice-pack"
SHAREABLE_DIR = VOICE_PACK_ROOT / "shareable"
PERSONAL_DIR = VOICE_PACK_ROOT / "personal"
OPEN_THREADS = Path.home() / ".narada" / "open-threads" / "open-threads.md"

SHAREABLE_CAP = 2000
PERSONAL_CAP = 4500
THREAD_HEADLINES_MAX = 6


def _read_dir(root: Path, cap: int) -> str:
    """Concatenate *.md directly inside `root` (sorted, no recursion —
    a nested surprise can't smuggle content), refusing symlinked files
    and anything that resolves outside the root."""
    if not root.is_dir():
        return ""
    parts: list[str] = []
    total = 0
    try:
        rroot = root.resolve()
    except OSError:
        return ""
    for f in sorted(root.glob("*.md")):
        try:
            if f.is_symlink() or not f.resolve().is_relative_to(rroot):
                logger.warning("pack: refusing non-local file %s", f)
                continue
            text = f.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not text:
            continue
        take = text[: max(0, cap - total)]
        if take:
            parts.append(take)
            total += len(take)
        if total >= cap:
            break
    return "\n\n".join(parts)


def build_shareable() -> str:
    """The every-session pack. Opens voice-pack/shareable/ ONLY."""
    return _read_dir(SHAREABLE_DIR, SHAREABLE_CAP)


def _thread_headlines() -> str:
    """Top open-thread HEADLINES only (visitor-tolerable by the wall
    calendar rule — titles, never bodies)."""
    try:
        lines = OPEN_THREADS.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    heads = [ln.lstrip("# ").strip() for ln in lines
             if ln.startswith("#") and ln.lstrip("# ").strip()]
    if not heads:
        return ""
    return "Open threads: " + "; ".join(heads[:THREAD_HEADLINES_MAX])


def build_personal() -> str:
    """The verified-tap pack: shareable + personal dir + thread
    headlines. Called ONLY after tier admission — the caller (worker)
    holds that gate; this function just refuses to be the shareable
    builder's code path."""
    parts = [build_shareable(),
             _read_dir(PERSONAL_DIR, PERSONAL_CAP),
             _thread_headlines()]
    return "\n\n".join(p for p in parts if p)


def build_for_tier(tier: str) -> str:
    return build_personal() if tier == "personal" else build_shareable()
