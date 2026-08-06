"""Shared-secret tokens for the session-manager surfaces.

Three tokens live in ``~/.narada/.sessions-tokens.json`` (protected by
the user-profile ACL, like every other secret under ~/.narada):

- ``voice``   — authorizes launching the voice-tier MCP server
- ``prana``   — authorizes launching the prana-tier MCP server
- ``service`` — authorizes requests to the persistent session service

Existing files missing a key (older layouts) are upgraded in place.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

TOKENS_FILE = Path.home() / ".narada" / ".sessions-tokens.json"
TOKEN_KEYS = ("voice", "prana", "service")

_LOCK_TIMEOUT_S = 10.0


@contextmanager
def _file_lock(path: Path):
    """Cross-process lock via O_EXCL lockfile (works on Windows).

    The host starts the service and MCP consumers together; without
    this, concurrent first-run initialization can issue different
    tokens to different processes (Codex recheck finding).
    """
    lock = path.with_suffix(path.suffix + ".lock")
    deadline = time.monotonic() + _LOCK_TIMEOUT_S
    fd = None
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                # Stale lock (crashed holder): steal after timeout.
                logger.warning("stealing stale tokens lock %s", lock)
                try:
                    lock.unlink()
                except OSError:
                    pass
                deadline = time.monotonic() + _LOCK_TIMEOUT_S
            time.sleep(0.05)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        try:
            lock.unlink()
        except OSError:
            pass


def _read(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def load_or_create_tokens(path: Path = TOKENS_FILE) -> dict[str, str]:
    tokens = _read(path)
    if all(tokens.get(k) for k in TOKEN_KEYS):
        return tokens
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(path):
        tokens = _read(path)  # re-read under the lock: a peer may have won
        missing = [k for k in TOKEN_KEYS if not tokens.get(k)]
        if not missing:
            return tokens
        for key in missing:
            tokens[key] = secrets.token_urlsafe(32)
        # Atomic replace so a reader never sees a truncated file.
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name, suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(tokens, f, indent=2)
            os.replace(tmp, str(path))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        logger.info("tokens file updated (%s) at %s", ", ".join(missing), path)
    return tokens
