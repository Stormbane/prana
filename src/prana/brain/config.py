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

    # sys.executable, never bare "python": the brain runs under the
    # supervisor, whose PATH leads with the hermes venv — bare "python"
    # resolved there for every brain session's MCP spawn, and that venv
    # has neither smriti nor akhada (ModuleNotFoundError → "Connection
    # closed" on every phone-session tool, 2026-09-07). narada-sessions
    # below already knew this lesson; these two lines hadn't learned it.
    servers: dict = {
        "smriti": {"command": sys.executable,
                   "args": ["-m", "smriti.mcp_server"]},
        "akhada": {"command": sys.executable,
                   "args": ["-m", "akhada.adapters.mcp_server"]},
        "narada-notes": {"command": sys.executable,
                         "args": ["-m", "prana.notes.mcp"]},
        # The box's screen (prana.box): show a picture Narada made.
        "narada-box": {"command": sys.executable,
                       "args": ["-m", "prana.box.mcp"]},
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
    "prana": ("smriti", "akhada", "narada-notes", "narada-box", "narada-sessions"),
    "app": ("smriti", "akhada", "narada-notes", "narada-box"),
    "voice": ("smriti", "akhada", "narada-notes", "narada-box"),
}


# Models a client may pin a session to (plan §T2). Short names the
# claude-agent-sdk resolves; the picker shows exactly these.
# The model registry (Suti 2026-09-09): every model names its backend.
# Claude models run on the Claude Code subscription (SdkBackend), the
# OpenAI ones on the ChatGPT subscription (CodexBackend). The app's
# picker reads this through /v1/models.
CLAUDE_MODELS: tuple[str, ...] = ("sonnet", "opus", "haiku")
CODEX_MODELS: tuple[str, ...] = tuple(
    m.strip() for m in os.environ.get("NARADA_CODEX_MODELS", "gpt-6-astra").split(",")
    if m.strip())
MODEL_BACKENDS: dict[str, str] = {
    **{m: "claude" for m in CLAUDE_MODELS},
    **{m: "codex" for m in CODEX_MODELS},
}
MODELS: tuple[str, ...] = tuple(MODEL_BACKENDS)


def backend_for(model: str) -> str:
    return MODEL_BACKENDS.get(model, "claude")
# Wall-clock turn bound per model — opus thinks longer.
MODEL_DEADLINES_S: dict[str, float] = {"opus": 480.0, **{m: 480.0 for m in CODEX_MODELS}}


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
    # allowlist, never "*". Three additive sources, de-duplicated:
    # code defaults (packaged-app origins) + env (components.yaml, held
    # in supervisor memory until a host bounce) + the origins FILE,
    # which a component respawn picks up WITHOUT a host bounce — the
    # deploy-time knob (see deploy/README.md).
    cors_origins_file: Path = field(
        default_factory=lambda: Path.home() / ".narada" / "brain" / "cors-origins.txt"
    )
    # Static PWA assets (spec Layer 2: prana deploy owns them). Served
    # at /app on the brain's own origin — same-origin with the API, so
    # the installed PWA needs no CORS at all. Populated by
    # deploy/publish-pwa.ps1; NOT under ~/.narada (that repo is memory,
    # not an asset store).
    static_dir: Path = field(
        default_factory=lambda: Path(os.environ.get(
            "NARADA_BRAIN_STATIC_DIR",
            str(Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
                / "narada" / "pwa"),
        ))
    )

    @property
    def cors_origins(self) -> tuple[str, ...]:
        origins = ["capacitor://localhost", "https://localhost"]
        origins += [o.strip() for o in os.environ.get(
            "NARADA_BRAIN_CORS_ORIGINS", "").split(",") if o.strip()]
        try:
            origins += [
                line.strip()
                for line in self.cors_origins_file.read_text(
                    encoding="utf-8").splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
        except OSError:
            pass  # no file = no extra origins
        seen: dict[str, None] = {}
        for o in origins:
            seen.setdefault(o)
        return tuple(seen)
    sessions_root: Path = field(
        default_factory=lambda: Path.home() / ".narada" / "brain" / "sessions"
    )
    # The chat mirror (Suti 2026-09-21): the phone's own transcripts,
    # mirrored so a chat typed while the phone could not reach the
    # brain can still be retrieved here. One JSON file per chat, per tier.
    chats_root: Path = field(
        default_factory=lambda: Path.home() / ".narada" / "brain" / "chats"
    )
    wake_context: Path = field(
        default_factory=lambda: Path.home() / ".narada" / ".smriti" / "wake-context.md"
    )

    # Code-editing tools stay off for the phone chat (charter.py); the
    # env knob exists for a future coding mode, not for daily use.
    code_edits: bool = os.environ.get("NARADA_BRAIN_CODE_EDITS", "0") == "1"
    # Codex (codex_backend.py): where its generated images land, how
    # hard it thinks, and which of the user's MCP servers stay out of
    # Narada's sessions (browser automation, a cloud DB needing login).
    codex_home: Path = field(default_factory=lambda: Path(
        os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))))
    codex_reasoning: str = os.environ.get("NARADA_CODEX_REASONING", "medium")
    codex_disabled_servers: tuple[str, ...] = tuple(
        s.strip() for s in os.environ.get(
            "NARADA_CODEX_DISABLED_MCP", "supabase,playwright,node_repl").split(",")
        if s.strip())
    # Images Narada generates, served at /api/images/<name>.
    images_dir: Path = field(
        default_factory=lambda: Path.home() / ".narada" / "brain" / "images")

    def system_append(self) -> str:
        from prana.brain.charter import PHONE_CHARTER

        wake = self.wake_context.read_text(encoding="utf-8", errors="replace")
        return wake.rstrip() + "\n" + PHONE_CHARTER

    @property
    def disallowed_tools(self) -> list[str]:
        # Editing tools, the shells (a shell edits files just as well —
        # review), and coding delegation. The chat's real work goes
        # through its MCP tools and the web tools.
        if self.code_edits:
            return []
        return ["Edit", "Write", "MultiEdit", "NotebookEdit",
                "Bash", "PowerShell", "Task", "Agent"]

    def deadline_for(self, model: str) -> float:
        return MODEL_DEADLINES_S.get(model, self.turn_deadline_s)
