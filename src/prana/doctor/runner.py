"""Run the checks concurrently, each under its own deadline, and render
or persist the report.

A check that raises or overruns becomes an AMBER "unknown" result: the
doctor not knowing is never reported as the subsystem being red, and
never as green either (the zombie-heartbeat rule: no fallback may
masquerade as a healthy answer).
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Callable, Iterable

from prana.doctor.checks import CHECKS, TITLES
from prana.doctor.model import AMBER, GREEN, RED, CheckResult, Report, redact
from prana.doctor.probes import Probes

DEFAULT_TIMEOUT_S = 20.0


def _run_one(cid: str, fn: Callable[[Probes], CheckResult], probes: Probes,
             slot: dict) -> None:
    started = time.monotonic()
    try:
        res = fn(probes)
        if not isinstance(res, CheckResult):
            raise TypeError(f"returned {type(res).__name__}, not a CheckResult")
    except Exception as exc:  # noqa: BLE001
        res = CheckResult(id=cid, title=TITLES.get(cid, cid), status=AMBER,
                          reason=redact(f"check crashed: {type(exc).__name__}: {exc}")[:200])
    res.duration_ms = int((time.monotonic() - started) * 1000)
    slot["result"] = res


def run_checks(probes: Probes,
               checks: Iterable[tuple[str, Callable[[Probes], CheckResult]]] = CHECKS,
               timeout_s: float = DEFAULT_TIMEOUT_S,
               only: set[str] | None = None) -> Report:
    selected = [(cid, fn) for cid, fn in checks if not only or cid in only]
    slots: list[tuple[str, dict, threading.Thread]] = []
    for cid, fn in selected:
        slot: dict = {}
        # Daemon threads: a wedged probe can never hold the process open.
        t = threading.Thread(target=_run_one, args=(cid, fn, probes, slot),
                             name=f"doctor-{cid}", daemon=True)
        t.start()
        slots.append((cid, slot, t))
    deadline = time.monotonic() + timeout_s
    results: list[CheckResult] = []
    for cid, slot, t in slots:
        t.join(max(0.0, deadline - time.monotonic()))
        if "result" in slot:
            results.append(slot["result"])
        else:
            results.append(CheckResult(
                id=cid, title=TITLES.get(cid, cid), status=AMBER,
                reason=f"check timed out after {timeout_s:.0f}s",
                duration_ms=int(timeout_s * 1000)))
    return Report(generated_at=probes.now(), checks=results, host=socket.gethostname())


_ICON = {GREEN: "ok ", AMBER: "!! ", RED: "XX "}
_COLOR = {GREEN: "\x1b[32m", AMBER: "\x1b[33m", RED: "\x1b[31m"}


def render_text(report: Report, probes: Probes, color: bool = False) -> str:
    def paint(status: str, text: str) -> str:
        return f"{_COLOR[status]}{text}\x1b[0m" if color else text

    counts = report.counts()
    when = report.generated_at.astimezone(probes.local_tz).strftime("%Y-%m-%d %H:%M %Z")
    lines = [
        f"narada doctor  {when}  overall: {paint(report.overall, report.overall.upper())}"
        f"  ({counts[RED]} red, {counts[AMBER]} amber, {counts[GREEN]} green)",
        "",
    ]
    width = max((len(c.title) for c in report.checks), default=10)
    for c in report.ordered():
        since = ""
        if c.since:
            stamp = c.since.astimezone(probes.local_tz).strftime("%Y-%m-%d %H:%M")
            since = f"  [since {'~' if c.since_approx else ''}{stamp}]"
        label = paint(c.status, f"{c.status.upper():<5}")
        lines.append(f"{label} {c.title:<{width}}  {c.reason}{since}")
        if c.hint and c.status != GREEN:
            lines.append(f"{'':<5} {'':<{width}}  -> {c.hint}")
    return redact("\n".join(lines))


def write_json(report: Report, path: Path) -> Path:
    """Atomic write (temp + replace) so the app never reads half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, path)
    return path
