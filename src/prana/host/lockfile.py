"""Single-instance lockfile so we don't double-launch the orchestrator.

Format mirrors Hermes's convention so ops familiarity carries over:

    {"pid": int, "kind": "prana.host", "argv": [...], "start_time": "ISO8601"}

API:
  - acquire(replace=False) -> True if lock taken; False if held by live pid
  - release() -> erase lock
  - read_lock() -> dict or None

`replace=True` kills the holder before taking the lock.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Tolerance when comparing the lockfile's recorded start_time against
# psutil.create_time(). The lockfile is written milliseconds-to-seconds
# after the process actually starts, so allow a small window.
_START_TIME_MATCH_TOLERANCE_S = 5.0

try:
    import psutil  # type: ignore
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

from prana.host.paths import lockfile_path

logger = logging.getLogger(__name__)


def read_lock() -> dict | None:
    p = lockfile_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("lockfile %s unreadable: %s — treating as absent", p, exc)
        return None


def _pid_alive(pid: int) -> bool:
    """Cross-platform check."""
    if pid <= 0:
        return False
    if HAS_PSUTIL:
        try:
            return psutil.pid_exists(pid) and psutil.Process(pid).is_running()
        except psutil.Error:
            return False
    # POSIX fallback. Windows without psutil: assume not alive (best-effort)
    if os.name == "posix":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    return False


def _lock_holder_alive(lock: dict) -> bool:
    """True only if the process recorded in `lock` is still the live holder.

    Stricter than ``_pid_alive``: also verifies the process's create time
    matches the lockfile's recorded ``start_time`` (within tolerance).
    PIDs are recycled — Windows in particular cycles them aggressively —
    so a bare pid liveness check can mistake an unrelated new process for
    the original holder and refuse to start the new host.

    If we can't read the process's start time (no psutil, or psutil
    raises), fall back to the bare pid check. Lockfiles missing the
    ``start_time`` field also fall back, for backward compatibility.
    """
    pid = int(lock.get("pid", 0))
    if not _pid_alive(pid):
        return False

    recorded = lock.get("start_time")
    if not recorded or not HAS_PSUTIL:
        return True  # best-effort: pid liveness only

    try:
        recorded_dt = datetime.fromisoformat(recorded)
    except ValueError:
        return True

    # Normalize to UTC-aware so timestamp arithmetic is unambiguous.
    if recorded_dt.tzinfo is None:
        recorded_dt = recorded_dt.replace(tzinfo=timezone.utc)
    recorded_epoch = recorded_dt.timestamp()

    try:
        actual_epoch = psutil.Process(pid).create_time()
    except psutil.Error:
        return False  # process vanished between checks; treat as gone

    return abs(actual_epoch - recorded_epoch) <= _START_TIME_MATCH_TOLERANCE_S


def _kill_pid(pid: int, timeout_s: float = 5.0) -> bool:
    """Try graceful TERM then KILL. Returns True if pid is now gone."""
    if not _pid_alive(pid):
        return True
    if HAS_PSUTIL:
        try:
            p = psutil.Process(pid)
            p.terminate()
            try:
                p.wait(timeout=timeout_s)
                return True
            except psutil.TimeoutExpired:
                p.kill()
                try:
                    p.wait(timeout=2.0)
                except psutil.TimeoutExpired:
                    pass
                return not _pid_alive(pid)
        except psutil.Error:
            return not _pid_alive(pid)
    if os.name == "posix":
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(int(timeout_s * 10)):
                if not _pid_alive(pid):
                    return True
                time.sleep(0.1)
            os.kill(pid, signal.SIGKILL)
            return not _pid_alive(pid)
        except OSError:
            return True
    return False


def acquire(replace: bool = False) -> bool:
    """Take the lock. Returns True on success, False if held by a live pid.

    With replace=True, kills the holder and takes the lock.
    """
    existing = read_lock()
    if existing:
        pid = int(existing.get("pid", 0))
        if _lock_holder_alive(existing):
            if not replace:
                logger.warning(
                    "host orchestrator already running (pid=%d, started %s)",
                    pid, existing.get("start_time", "?"),
                )
                return False
            logger.warning("--replace: killing existing host orchestrator pid=%d", pid)
            if not _kill_pid(pid):
                logger.error("failed to kill existing orchestrator pid=%d", pid)
                return False
        elif _pid_alive(pid):
            # PID is alive but doesn't match the recorded start_time —
            # a different process inherited this pid. Original is gone.
            logger.info(
                "stale lockfile (pid=%d alive but start_time mismatch — pid reused) — replacing",
                pid,
            )
        else:
            logger.info("stale lockfile (pid=%d not alive) — replacing", pid)

    p = lockfile_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "kind": "prana.host",
        "argv": list(sys.argv),
        "start_time": datetime.now(timezone.utc).isoformat(),
    }
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("host lock acquired: %s (pid=%d)", p, os.getpid())
    return True


def release() -> None:
    """Drop the lock if we hold it."""
    p = lockfile_path()
    existing = read_lock()
    if existing and int(existing.get("pid", 0)) == os.getpid():
        try:
            p.unlink()
            logger.info("host lock released")
        except OSError as exc:
            logger.warning("could not remove lockfile %s: %s", p, exc)
