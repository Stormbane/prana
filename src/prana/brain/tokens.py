"""Brain bearer tokens + the git-crypt storage contract (spec §1a).

``~/.narada`` is a versioned repo that is git-crypt-encrypted BY
DEFAULT (``** filter=git-crypt``); tracked secrets are ciphertext at
rest and on the remote. The startup check verifies that contract still
covers the tokens file and refuses to serve if it doesn't (fail
closed) — an unencrypted secrets file in a pushed repo must stop the
server, not become folklore.
"""

from __future__ import annotations

import secrets
import subprocess
from pathlib import Path

from prana.sessions.tokens import load_or_create_tokens

BRAIN_TOKENS_FILE = Path.home() / ".narada" / ".brain-tokens.json"
TIERS = ("prana", "app", "voice")


def load_brain_tokens(path: Path = BRAIN_TOKENS_FILE) -> dict[str, str]:
    """Verify the encryption contract BEFORE any token generation — a
    first run must never drop plaintext secrets into the versioned repo
    and only then discover git-crypt is broken (diff review 2026-09-05)."""
    verify_gitcrypt_covers(path)
    return load_or_create_tokens(path, keys=TIERS)


def tier_for_token(presented: str, tokens: dict[str, str]) -> str | None:
    """Constant-time comparison against every tier; None = unauthorized."""
    matched = None
    for tier in TIERS:
        expected = tokens.get(tier, "")
        if expected and secrets.compare_digest(presented, expected):
            matched = tier
    return matched


def verify_gitcrypt_covers(path: Path = BRAIN_TOKENS_FILE) -> None:
    """Raise RuntimeError unless git-crypt's filter applies to ``path``."""
    repo = path.parent
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "check-attr", "filter", "--", path.name],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"cannot verify git-crypt attribute: {exc}") from exc
    if out.returncode != 0 or "filter: git-crypt" not in out.stdout:
        raise RuntimeError(
            f"{path} is NOT covered by git-crypt in {repo} "
            f"(check-attr said: {out.stdout.strip() or out.stderr.strip()!r}); "
            "refusing to store bearer tokens in a pushed repo as cleartext"
        )
