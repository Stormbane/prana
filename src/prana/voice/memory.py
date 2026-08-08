"""Voice-safe memory projection (cross-review #1).

The voice tier must never recall private memory: anyone in earshot could
otherwise make Narada speak sensitive memories aloud, and everything
recalled is sent to the model provider. This searches ONLY an explicit
allowlist of branch directories under ~/.narada. Private branches
(people, journal, identity, mind, open-threads, ...) are *physically
never opened* — deny-by-default by construction, not by post-filtering,
so nothing private can leak even on a bug.

For genuinely private recall, the path is the authenticated surfaces
(chat bridge / escalate-to-prana), never the open voice surface.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from prana.voice.transcripts import redact

logger = logging.getLogger(__name__)

NARADA_ROOT = Path.home() / ".narada"

# ONLY these branches are voice-recallable. Deny-by-default: any branch
# not listed here is never searched, never read, never spoken.
VOICE_RECALLABLE_BRANCHES: tuple[str, ...] = ("projects", "notes", "sources")

# Hard denylist — the private core. Asserted disjoint from the allowlist
# (see _assert_safe). Belt-and-suspenders: even if a branch were wrongly
# added to the allowlist, membership here makes recall() refuse it.
NEVER_RECALLABLE: frozenset[str] = frozenset({
    "people", "journal", "identity", "mind", "open-threads", "sacred",
    "goals", "feedback", "inbox", "outbox", "chat-sessions", "events",
    "threads", "episodes", "semantic", "days", "state", "logs", "host",
    "heartbeat", "mirrors", "test",
})

MAX_SNIPPET = 240


def _assert_safe(branches: Iterable[str]) -> list[str]:
    safe = []
    for b in branches:
        if b in NEVER_RECALLABLE:
            logger.error("refusing voice-recall of private branch %r", b)
            continue
        safe.append(b)
    return safe


@dataclass
class Memory:
    branch: str
    path: str
    snippet: str


def _score(query_terms: set[str], text: str) -> int:
    low = text.lower()
    return sum(1 for t in query_terms if t in low)


def recall(
    query: str,
    *,
    root: Path = NARADA_ROOT,
    branches: Iterable[str] = VOICE_RECALLABLE_BRANCHES,
    limit: int = 4,
) -> list[Memory]:
    """Keyword-search the allowlisted branches only. Returns redacted
    snippets, best match first. Never touches a non-allowlisted branch."""
    safe_branches = _assert_safe(branches)
    terms = {t for t in query.lower().split() if len(t) > 2}
    if not terms:
        return []
    hits: list[tuple[int, Memory]] = []
    for branch in safe_branches:
        bdir = root / branch
        if not bdir.is_dir():
            continue
        for md in bdir.rglob("*.md"):
            try:
                text = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            s = _score(terms, text)
            if s == 0:
                continue
            # snippet = the best-matching paragraph, redacted
            snippet = _best_paragraph(terms, text)
            hits.append((s, Memory(branch=branch, path=md.name,
                                   snippet=redact(snippet)[:MAX_SNIPPET])))
    hits.sort(key=lambda x: x[0], reverse=True)
    return [m for _, m in hits[:limit]]


def _best_paragraph(terms: set[str], text: str) -> str:
    best, best_s = "", -1
    for para in text.split("\n\n"):
        para = para.strip()
        if not para or para.startswith("---"):
            continue
        s = _score(terms, para)
        if s > best_s:
            best, best_s = para, s
    return " ".join(best.split())
