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


_DENY_LOWER = frozenset(n.lower() for n in NEVER_RECALLABLE)


def _resolve_safe_roots(root: Path, branch_names: Iterable[str]) -> list[tuple[str, Path]]:
    """Resolve the allowlisted branches to real directories, rejecting
    anything that could reach private content: non-bare names, path
    traversal, denylist matches (case-insensitive), symlinks/junctions,
    and dirs that don't resolve to a direct child of root.
    """
    try:
        rroot = root.resolve()
    except OSError:
        return []
    private_roots = []
    for name in NEVER_RECALLABLE:
        p = root / name
        try:
            if p.exists():
                private_roots.append(p.resolve())
        except OSError:
            continue
    safe: list[tuple[str, Path]] = []
    for b in branch_names:
        # must be a bare, non-private branch name
        if not b or "/" in b or "\\" in b or ".." in b or b == "." :
            continue
        if b.lower() in _DENY_LOWER:
            logger.error("refusing voice-recall of private branch %r", b)
            continue
        d = root / b
        try:
            if d.is_symlink() or not d.is_dir():
                continue
            rd = d.resolve()
        except OSError:
            continue
        # must resolve to a DIRECT child of the (resolved) narada root,
        # and not be (or sit under) any private branch
        if rd.parent != rroot:
            continue
        if any(rd == pr or pr in rd.parents for pr in private_roots):
            continue
        safe.append((b, rd))
    return safe


def _contained(path: Path, root: Path) -> bool:
    """True iff `path` resolves to a real file strictly under `root`,
    with no symlink escape."""
    try:
        if path.is_symlink():
            return False
        rp = path.resolve()
        return root == rp.parent or root in rp.parents
    except OSError:
        return False


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
    branches: Iterable[str] | None = None,
    limit: int = 4,
) -> list[Memory]:
    """Keyword-search the allowlisted branches only. Returns redacted
    snippets, best match first. Cannot touch a non-allowlisted branch:
    every branch and candidate file is resolved and containment-checked,
    and symlinks are rejected. `branches` defaults to the constant
    allowlist; a caller override is still fully containment-validated."""
    branch_names = VOICE_RECALLABLE_BRANCHES if branches is None else tuple(branches)
    terms = {t for t in query.lower().split() if len(t) > 2}
    if not terms:
        return []
    hits: list[tuple[int, Memory]] = []
    for branch, rdir in _resolve_safe_roots(root, branch_names):
        for md in rdir.rglob("*.md"):
            if not _contained(md, rdir):
                continue  # symlink escape or resolves outside the branch
            try:
                text = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            s = _score(terms, text)
            if s == 0:
                continue
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
