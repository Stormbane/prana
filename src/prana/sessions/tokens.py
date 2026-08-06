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
import secrets
from pathlib import Path

logger = logging.getLogger(__name__)

TOKENS_FILE = Path.home() / ".narada" / ".sessions-tokens.json"
TOKEN_KEYS = ("voice", "prana", "service")


def load_or_create_tokens(path: Path = TOKENS_FILE) -> dict[str, str]:
    tokens: dict[str, str] = {}
    if path.exists():
        try:
            tokens = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            logger.warning("tokens file unreadable; regenerating")
            tokens = {}
    missing = [k for k in TOKEN_KEYS if not tokens.get(k)]
    if missing:
        for key in missing:
            tokens[key] = secrets.token_urlsafe(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
        logger.info("tokens file updated (%s) at %s", ", ".join(missing), path)
    return tokens
