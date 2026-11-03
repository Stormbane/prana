"""`GET /api/doctor`: the doctor's status card, the one door the app uses.

The card (`narada.status-card` v1, docs/doctor.md) is self-describing:
the app renders it without knowing any check, so a new or changed check
needs no app release. The brain runs the same `run_checks` the CLI runs,
in a worker thread so its own /health probe of this server still gets
answered, at most once a minute however often the app asks, and writes
~/.narada/host/doctor.json each time so the wake briefing and the digest
read the same answer.
"""
from __future__ import annotations

import asyncio
import logging
import time

from fastapi import Depends, HTTPException

logger = logging.getLogger(__name__)

MAX_AGE_S = 60.0       # a cached card younger than this is served as is
FRESH_MIN_AGE_S = 10.0  # ?fresh=1 (the app's pull-to-refresh) still waits this long
ALLOWED_TIERS = ("prana", "app")  # not voice: the card names host internals


def register_doctor(app, require_tier, *, run=None, clock=time.monotonic) -> None:
    """`run` returns a prana.doctor Report; injectable for tests."""
    state: dict = {"at": None, "card": None}
    lock = asyncio.Lock()

    def _run_and_write():
        from prana.doctor.probes import Probes
        from prana.doctor.runner import run_checks, write_json
        probes = Probes()
        report = run_checks(probes)
        try:
            write_json(report, probes.paths.doctor_json)
        except OSError as exc:  # the card is still worth serving
            logger.warning("doctor.json not written: %s", exc)
        return report

    runner = run or _run_and_write

    @app.get("/api/doctor")
    async def doctor(fresh: bool = False, tier: str = Depends(require_tier)) -> dict:
        if tier not in ALLOWED_TIERS:
            raise HTTPException(403, "the doctor card is for the app and prana tiers")
        async with lock:  # one run at a time; concurrent callers share it
            age = None if state["at"] is None else clock() - state["at"]
            stale = age is None or age > MAX_AGE_S or (fresh and age > FRESH_MIN_AGE_S)
            if stale:
                try:
                    report = await asyncio.to_thread(runner)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("doctor run failed: %s", exc)
                    if state["card"] is None:
                        raise HTTPException(502, f"doctor failed: {type(exc).__name__}")
                    return state["card"]  # the last card, honestly dated
                state.update(at=clock(), card=report.to_card())
            return state["card"]
