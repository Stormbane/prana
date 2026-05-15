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


def lockfile_path() -> Path:
    """Single-instance lockfile so we don't double-launch."""
    return state_dir() / "host.lock"
