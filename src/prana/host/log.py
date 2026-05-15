"""Unified host logging — one file, prefix-tagged lines.

`2026-05-11 14:03:27 [agent-gateway] message`

Rotating: 10 MB × 5 files. Verbose mode also tees to stderr.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from prana.host.paths import log_dir


HOST_LOG_NAME = "host.log"
MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5


def setup_logging(*, verbose: bool = False) -> Path:
    """Configure the root logger. Idempotent — safe to call twice.

    Returns the path of the active log file.
    """
    logdir = log_dir()
    logdir.mkdir(parents=True, exist_ok=True)
    logpath = logdir / HOST_LOG_NAME

    root = logging.getLogger()
    # Clear any pre-existing handlers from a previous setup so reconfig
    # in the same process doesn't double-log.
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        logpath, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    if verbose:
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        root.addHandler(stream)

    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    return logpath


def component_logger(name: str) -> logging.Logger:
    """Get a logger that prefixes its name with [component]."""
    return logging.getLogger(f"prana.host.[{name}]")
