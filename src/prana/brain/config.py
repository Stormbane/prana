"""Brain-server configuration — env-tunable, spec §1a defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _mcp_servers() -> dict:
    """The full (prana-tier) MCP wiring — same set the chat bridge
    proved out. Reduced tiers subset this at session creation."""
    import sys

    from prana.sessions.tokens import load_or_create_tokens

    servers: dict = {
        "smriti": {"command": "python", "args": ["-m", "smriti.mcp_server"]},
        "akhada": {"command": "python", "args": ["-m", "akhada.adapters.mcp_server"]},
    }
    try:
        token = load_or_create_tokens()["prana"]
        servers["narada-sessions"] = {
            "command": sys.executable,
            "args": ["-m", "prana.sessions.mcp", "--tier", "prana"],
            "env": {"PRANA_SESSIONS_TOKEN": token},
        }
    except Exception:
        # Session tools are optional for chat; memory + fitness are not.
        pass
    return servers


# Which MCP servers each caller tier gets wired with (spec §1a auth:
# "the tier decides which MCP tools the session's agent is wired with").
TIER_SERVERS: dict[str, tuple[str, ...]] = {
    "prana": ("smriti", "akhada", "narada-sessions"),
    "app": ("smriti", "akhada"),
    "voice": ("smriti", "akhada"),
}


@dataclass
class BrainConfig:
    host: str = os.environ.get("NARADA_BRAIN_HOST", "127.0.0.1")
    # 8811: clear of the 879x cluster — the LiveKit agents workers grab
    # undocumented ports there (8793 turned out to be the live voice
    # worker's; discovered by collision 2026-09-05).
    port: int = int(os.environ.get("NARADA_BRAIN_PORT", "8811"))
    model: str = os.environ.get("NARADA_BRAIN_MODEL", "sonnet")
    max_tool_iterations: int = int(os.environ.get("NARADA_BRAIN_MAX_TURNS", "20"))
    turn_deadline_s: float = float(os.environ.get("NARADA_BRAIN_DEADLINE_S", "240"))
    max_concurrent_sessions: int = int(os.environ.get("NARADA_BRAIN_CONCURRENCY", "4"))
    idle_ttl_s: float = float(os.environ.get("NARADA_BRAIN_IDLE_TTL_S", str(60 * 60)))
    idempotency_window: int = 8
    # Browser transport contract (spec Layer 2): explicit origin
    # allowlist, never "*". Packaged-app origins are the code default;
    # the tailnet origin(s) ride in via env from the deploy config
    # (components.yaml), keeping the ts.net name out of the codebase.
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            o.strip() for o in os.environ.get(
                "NARADA_BRAIN_CORS_ORIGINS",
                "capacitor://localhost,https://localhost",
            ).split(",") if o.strip()
        )
    )
    sessions_root: Path = field(
        default_factory=lambda: Path.home() / ".narada" / "brain" / "sessions"
    )
    wake_context: Path = field(
        default_factory=lambda: Path.home() / ".narada" / ".smriti" / "wake-context.md"
    )

    def system_append(self) -> str:
        return self.wake_context.read_text(encoding="utf-8", errors="replace")
