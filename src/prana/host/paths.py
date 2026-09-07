"""Canonical paths for host orchestrator state — config, lockfile, logs."""

from __future__ import annotations

import os
from pathlib import Path


# Component registry — YAML config. User-editable.
NARADA_HOST_DIR = Path.home() / ".narada" / "host"
COMPONENTS_YAML = NARADA_HOST_DIR / "components.yaml"


def state_dir() -> Path:
    """Where mutable runtime state goes (lockfile, etc.).

    On Windows: %LOCALAPPDATA%/narada/ — appropriate for non-roaming
    machine-local state. Falls back to ~/.narada/ on POSIX or if
    LOCALAPPDATA is unset.
    """
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "narada"
    return Path.home() / ".narada"


def log_dir() -> Path:
    """Where host.log and per-component logs go."""
    return state_dir() / "logs"


def heartbeat_file() -> Path:
    """Liveness beacon the external deadman check watches.

    Written by the supervisor's event loop every HEARTBEAT_INTERVAL_S;
    staleness (not process existence) is the deadman's hang signal, so
    a wedged event loop is indistinguishable from a dead host — by
    design (platform spec, one-process addendum, singleton 2).
    """
    return state_dir() / "host.heartbeat"


def deadman_progress_file() -> Path:
    """Beacon the DEADMAN writes each tick, so the host can watch that
    the deadman (and therefore Task Scheduler) is still firing.

    The reverse of the heartbeat: the deadman is a scheduled task, the
    host is a long-running process. Each watches the other from a
    different failure domain (retire-hermes plan, §Scheduler observer).
    The host reads this file's age; if stale, Task Scheduler has stopped
    firing the deadman and the host pages — the observer of the observer.
    """
    return state_dir() / "deadman.progress"


def lockfile_path() -> Path:
    """Single-instance lockfile so we don't double-launch."""
    return state_dir() / "host.lock"
