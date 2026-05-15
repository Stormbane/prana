# Host Orchestrator — Plan

**Date:** 2026-05-11
**Owner:** prana
**Status:** plan, not yet implemented

## Problem

Narada's runtime on a single host is currently fragmented across:

- **Hermes gateway** — autostarts via `~/.hermes/gateway-service/Hermes_Gateway.cmd`,
  registered in Windows Startup folder. Owns chat I/O for some platforms.
- **narada_chat_bridge.py** — autostarts via `Narada_Chat_Bridge.cmd` in
  Startup folder. Per-message `claude -p --continue` for Telegram. Bypasses
  Hermes entirely for chat.
- **prana heartbeat daemon** — launched manually by `scripts/heartbeat.bat`,
  whose `:loop` is the only supervisor. No autostart on this machine.
- **deha voice supervisor** — launched manually. Has its own internal
  supervisor for HA container + brain_server child. No autostart on this
  machine.
- **Ollama** — autostarts via `Ollama.lnk` in Startup folder. External dep.

Three problems compound:

1. **Not invisible.** Startup-folder `.cmd`s pop console windows on log-on.
2. **Not reliable.** No restart-on-crash for heartbeat or deha. The
   heartbeat batch-loop doesn't survive console close.
3. **Not portable.** Onboarding a second machine means hand-installing
   four startup shortcuts, three .cmd files, and remembering deha exists.

Also: agent framework lock-in. We currently run Hermes for orchestration,
but the long-term stance is *the agent framework should be pluggable*.
Today's Hermes might be tomorrow's LangGraph / Swarm / something else.
The host orchestrator is the right seam to enforce that.

## Goal

A single supervised process tree on the host that brings up all of
Narada's runtime components — voice, heartbeat, chat bridge, agent
gateway — invisibly, reliably, and reproducibly on a fresh machine.

## Where it lives

`prana/src/prana/host/` — supervisor + component registry + lifecycle.
CLI: `prana host run | install | uninstall | status`.

prana already has the operational primitives (sync subprocess, state.db,
signal handling). What's missing is the multi-component supervisor layer.
Adding it to prana fits the project's identity: prana is the life-force,
the always-on process that keeps Narada alive on the host.

**Layering rule preserved.** prana spawns deha and hermes as peer
subprocesses; no Python-level imports across project boundaries.
deha's `supervisor.py` stays in place for standalone-deha dev runs
(testing, debugging, headless host without prana). The duplication of
~150 LOC of subprocess management is acceptable — keeps the no-dep
layering rule in `deha/CLAUDE.md` honest.

## Architecture

### Component model

Every supervised process is a `Component`:

```python
@dataclass(frozen=True)
class Component:
    name: str
    command: list[str]
    cwd: Path
    env: dict[str, str]              # merged into os.environ at spawn
    restart_grace_s: float = 2.0
    enabled: bool = True
    health_url: str | None = None    # optional HTTP /health for liveness
    health_interval_s: float = 30.0
    description: str = ""
```

Components live in YAML, not Python — so swapping Hermes for another
agent framework is a config edit, not a code change:

```yaml
# ~/.narada/host/components.yaml
components:
  - name: agent-gateway
    command: [python, -m, hermes_cli.main, gateway, run, --replace]
    cwd: C:\Projects\hermes-spike\hermes-agent
    env:
      HERMES_HOME: ${HOME}\.hermes
      PYTHONIOENCODING: utf-8
    health_url: null
    description: "Hermes agent gateway (chat orchestrator)"

  - name: heartbeat
    command: [python, -m, prana.heartbeat, --interval, "1800"]
    cwd: ${PRANA_ROOT}
    env: {}
    description: "30-min reflection cycle"

  - name: chat-bridge
    command: [python, scripts/narada_chat_bridge.py]
    cwd: ${PRANA_ROOT}
    env: {}
    description: "Telegram bot (claude -p per message)"

  - name: deha-brain
    command:
      - python
      - -m
      - deha.voice.supervisor
      - --voice
      - am_michael:0.5,af_heart:0.5
    cwd: ${DEHA_ROOT}
    env: {}
    health_url: http://127.0.0.1:8765/health
    description: "Voice brain + Wyoming TTS + HA conversation"
```

`${VAR}` substitution from a small set of host vars: `HOME`,
`PRANA_ROOT`, `DEHA_ROOT`, `HERMES_HOME`, `LOCALAPPDATA`. Resolved at
load time, not at spawn time.

### Supervisor responsibilities

1. **Spawn each enabled component** as a subprocess. Capture stdout +
   stderr via pipes; tag each line with `[name]` prefix; pump into
   unified log.
2. **Restart on exit.** Wait `restart_grace_s` after a clean or dirty
   exit, then respawn. Crash rate limiter: more than 3 exits in 60 s
   → disable for 5 minutes, log loud.
3. **Health checks.** For components advertising `health_url`, poll on
   `health_interval_s`. Three consecutive failures → SIGTERM + restart.
4. **Graceful shutdown.** On SIGINT/SIGTERM to the orchestrator: send
   SIGTERM to every child, wait up to 5 s, SIGKILL stragglers.
5. **Orphan killer on startup.** Use a lockfile (`%LOCALAPPDATA%/narada/host.lock`)
   storing `{pid, start_time}`. On startup, if the lock exists and the
   PID is alive, refuse to start (or kill if `--replace`). Pattern
   already used by Hermes (`~/.hermes/gateway.pid`).

Lift the patterns that already work in `deha/src/deha/voice/supervisor.py`:
- per-component `restart_grace_s` (HA 60 s, brain_server 10 s)
- orphan-killer keyed on a marker arg in the cmdline
- prefix-tagged log capture from subprocess pipes
- single rotated log file

### Logging

- One unified file: `%LOCALAPPDATA%/narada/logs/host.log`
- Format: `2026-05-11 14:03:27 [agent-gateway] message`
- `RotatingFileHandler`: 10 MB × 5 files (50 MB cap).
- Per-component crash: drain stderr buffer into the log on exit, with a
  big visible boundary (`====== chat-bridge crashed (rc=1) ======`).
- Verbose mode (`prana host run --verbose`) tees to stderr too.

### Invisibility on Windows

- Launched via Windows **Task Scheduler "At log on"**, not Startup
  folder. Three reasons: hidden console window, restart-on-failure
  built in, no flicker on log-on.
- Run inside `pythonw.exe` (no console). The `prana host run` console
  script will detect a missing TTY and not try to print to stderr.
- Single `.cmd` shim is for the install path only — Task Scheduler
  invokes `pythonw.exe -m prana.host run` directly.

### Pluggability — the seam that matters

The agent-gateway abstraction is a single component config block. To
swap Hermes for another framework:
1. Edit `components.yaml` — change the `command`, `cwd`, `env` for the
   `agent-gateway` component.
2. (If the new framework has different chat plumbing) edit the
   `chat-bridge` component or replace it.
3. Restart the orchestrator.

No code changes in prana, deha, or hermes-agent. The constraint this
imposes: any agent framework must run as a long-lived subprocess with a
sane signal-handling story. Hermes already does. Most do.

## Installable on next computer

Two scripts under `prana/scripts/install/`:

- **`install.ps1`** — registers Task Scheduler entry "Narada Host"
  pointing at `pythonw.exe -m prana.host run`. Creates
  `%LOCALAPPDATA%/narada/logs/` and `%LOCALAPPDATA%/narada/host/`.
  Generates `~/.narada/host/components.yaml` from
  `prana/scripts/install/components.template.yaml` substituting paths.
- **`uninstall.ps1`** — removes the Task Scheduler entry + lockfile.

Wrapped by `prana host install` / `prana host uninstall` console
subcommands. Idempotent.

Pre-reqs documented in `docs/host-installation.md` (separate doc, written
during phase 5):
- Python 3.12 + per-project venvs (prana, deha)
- Ollama installed + `qwen3:8b` pulled
- Hermes installed (or whatever agent framework is configured)
- claude-cli logged in (`claude login`)
- Voice models present in `deha/models/`
- HA running (this orchestrator does **not** supervise HA — the deha
  internal supervisor still owns the HA container watchdog)

## Implementation phases

Each phase ends with a working state, not a half-finished commit.

### Phase 1 — Scaffold (1 component)

- `src/prana/host/__init__.py`, `host/cli.py`, `host/supervisor.py`,
  `host/component.py`, `host/log.py`
- YAML loader with `${VAR}` substitution
- Single component (heartbeat) supervised end-to-end: spawn, log
  capture, restart on crash
- Lockfile + orphan killer
- Console script: `prana host run`

**Done when:** `prana host run` brings up heartbeat. Killing heartbeat
externally → orchestrator respawns it. Ctrl-C on the orchestrator →
heartbeat dies cleanly.

### Phase 2 — Multi-component

- Add chat-bridge, agent-gateway, deha-brain to components.yaml
- Concurrent spawn + supervise
- Crash rate limiter (3 exits / 60 s → disable 5 min)

**Done when:** All four components run under one `prana host run`.
Killing any one → only that one restarts. Killing the orchestrator →
all four die.

### Phase 3 — Health checks

- Optional `health_url` polling per component
- 3-strikes-and-restart logic for unresponsive components
- Health state surfaced via `prana host status`

**Done when:** `curl http://127.0.0.1:8765/health` returning bad → deha-brain
gets SIGTERM'd and respawned within `health_interval_s + restart_grace_s`.

### Phase 4 — Install scripts

- `install.ps1` / `uninstall.ps1`
- `prana host install` / `prana host uninstall` console subcommands
- `components.template.yaml` with `${VAR}` placeholders
- Migration: remove `Hermes_Gateway.cmd` and `Narada_Chat_Bridge.cmd`
  from Startup folder (these are now supervised by the orchestrator).
  `Ollama.lnk` stays (external dep).

**Done when:** `prana host install` on a fresh machine + reboot →
everything comes up invisibly on log-on.

### Phase 5 — Docs

- `docs/host-orchestrator.md` (topology, ops, troubleshooting)
- `docs/host-installation.md` (pre-reqs + install steps)
- Update `prana/README.md` to mention the host role

## Out of scope for v1

- Cross-machine orchestration
- Containerization (Docker/Podman) — host-native is simpler now
- Web dashboard — `prana host status` + `host.log` is enough
- Auto-update — `git pull` per project, manual
- Linux/macOS support — Windows-first; the supervisor itself is
  cross-platform but install scripts are PowerShell

## Open questions to settle before implementing

1. **Does deha's internal supervisor stay or fold in?** Today
   `deha/supervisor.py` watches both brain_server and the HA container.
   Cleanest: it stays — it knows about HA-specific quirks (60 s grace,
   `/manifest.json` boot-time slowness). The host orchestrator
   supervises the deha supervisor process, not the brain_server
   directly. **Recommended.**

2. **Lockfile format.** Match Hermes's existing convention
   (`{"pid": int, "kind": str, "argv": [...], "start_time": null}`)
   for ops familiarity. **Recommended.**

3. **When does the orchestrator start ollama?** Currently `Ollama.lnk`
   in Startup folder. Question: should the orchestrator wait for
   Ollama to be reachable before starting agent-gateway, since Hermes
   needs Ollama? **Yes** — add a pre-start dependency: agent-gateway
   waits for `http://127.0.0.1:11434/api/tags` to succeed. Simple
   one-shot probe at component spawn time, not a recurring health
   check. Configurable via `wait_for_url` in the component spec.

4. **Per-component log isolation?** Currently planning one unified
   `host.log`. Alternative: per-component files. Unified is easier
   for cross-component debugging (which is most of what you'll do).
   Stick with unified; per-component view is a `grep "[name]"` away.
