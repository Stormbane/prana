"""Turn lifecycle — the idempotency state machine frozen in spec §1a.

A turn that carries a client ``request_id`` gets a durable record at
ACCEPT time (before the agent loop starts), bound to a fingerprint of
the request content. Retries with the same id + fingerprint replay the
recorded outcome; a different fingerprint under the same id is rejected;
records found non-terminal at load time become ``interrupted`` — the
server never guesses whether a dead turn's effects happened.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "interrupted"})


def fingerprint(session_id: str, model: str, prompt: str) -> str:
    canon = json.dumps(
        {"session": session_id, "model": model, "prompt": prompt},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


@dataclass
class TurnRecord:
    request_id: str
    fingerprint: str
    state: str = "accepted"  # accepted -> running -> terminal
    result: str | None = None  # final text (completed) or error detail
    created: float = field(default_factory=time.time)


class TurnStore:
    """Per-session durable store: ``turns.json`` in the session dir.

    Writes are atomic-replace (a reader never sees a truncated file) and
    happen on every transition — durability at ACCEPT is the point.
    """

    def __init__(self, path: Path, window: int = 8):
        self._path = path
        self._window = window
        self._records: dict[str, TurnRecord] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        recovered = False
        for item in raw:
            rec = TurnRecord(**item)
            if rec.state not in TERMINAL_STATES:
                # Crash recovery: the process died mid-turn. Explicitly
                # interrupted, never silently rerun or replayed.
                rec.state = "interrupted"
                rec.result = "turn was in flight when the server stopped"
                recovered = True
            self._records[rec.request_id] = rec
        if recovered:
            self._flush()

    def _flush(self) -> None:
        ordered = sorted(self._records.values(), key=lambda r: r.created)
        while len(ordered) > self._window:  # oldest-first eviction
            evicted = ordered.pop(0)
            del self._records[evicted.request_id]
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump([asdict(r) for r in ordered], f, ensure_ascii=False)
            os.replace(tmp, str(self._path))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def get(self, request_id: str) -> TurnRecord | None:
        return self._records.get(request_id)

    def accept(self, request_id: str, fp: str) -> TurnRecord:
        rec = TurnRecord(request_id=request_id, fingerprint=fp)
        self._records[request_id] = rec
        self._flush()
        return rec

    def transition(self, request_id: str, state: str, result: str | None = None) -> None:
        rec = self._records.get(request_id)
        if rec is None:
            return
        rec.state = state
        if result is not None:
            rec.result = result
        self._flush()
