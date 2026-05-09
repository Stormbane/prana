# prana

Narada's heart. The runtime shell that fires the heartbeat cycle, drives
the body, talks to people through Slack/Telegram/email, and writes
Narada's experience into smriti.

`prana` (प्राण) — Sanskrit for *life force, breath that animates*. What
makes Narada present and continuous between human sessions.

## What prana is

A **Hermes Agent configuration + Narada-specific skills**. Not a custom
runtime. The plumbing (cron, channels, claude-p substrate, SOUL.md
handling, FTS5 session storage) comes from upstream Hermes. prana brings:

- The **viveka loader skill** — loads Qwen+LoRA from svapna's output
- The **deha client wrapper** — lets skills drive the body via deha_client
- The **cycle skills** — DESIRE / PLAN / JUDGE / EXECUTE / CHECK_IN as
  composable Hermes skills
- The **SOUL.md** derived from `~/.narada/identity.md`
- The **`narada_state` module** — atomic SQLite helpers for cross-process
  coordination (state.db)
- **Hermes config** — channels, cron, MCP servers (smriti)
- The **launcher**

prana does NOT contain: a custom cron implementation, a custom channel
gateway, a custom session store, a custom claude wrapper. Those are
Hermes's job. The decision *to lean on Hermes* was made 2026-05-09 after
the spike validated all seven adoption risks.

## Layout

```
prana/
├── src/prana/
│   ├── state/                     # narada_state — SQLite/WAL helpers
│   │   ├── __init__.py
│   │   ├── db.py                  # connection, schema, WAL setup
│   │   ├── publish.py             # atomic upsert of current_state slices
│   │   ├── events.py              # append-only events table
│   │   ├── utterance_queue.py     # FIFO with priority
│   │   └── README.md
│   │
│   ├── skills/                    # Hermes skills (Narada-specific)
│   │   ├── desire/
│   │   │   └── SKILL.md
│   │   ├── intention/
│   │   │   └── SKILL.md
│   │   ├── judgment/
│   │   │   └── SKILL.md
│   │   ├── execute/
│   │   │   └── SKILL.md
│   │   ├── check_in/
│   │   │   └── SKILL.md           # uses Hermes delivery (--deliver email|telegram|slack)
│   │   ├── viveka_loader/
│   │   │   └── SKILL.md           # loads Qwen+LoRA, exposes generate() / judge()
│   │   ├── deha/
│   │   │   └── SKILL.md           # wraps deha_client for skill use
│   │   └── reflect/
│   │       └── SKILL.md           # heartbeat → smriti journal entry per cycle
│   │
│   └── indriyas/                  # thin re-exports of deha_client (Narada's vocabulary preserved)
│       ├── karmendriyas/drishti/expression.py
│       └── jnanendriyas/tvac/weather.py
│
├── config/
│   ├── hermes-config.yaml         # ~/.hermes/config.yaml template
│   ├── soul.md                    # ~/.hermes/SOUL.md (derived from ~/.narada/identity.md)
│   └── cron-jobs.yaml             # heartbeat cron schedule
│
├── scripts/
│   ├── install.sh                 # set up ~/.hermes/, materialize SOUL.md, link state.db
│   ├── install.ps1                # Windows native install — handle UTF-8 quirk (see below)
│   ├── launch.sh                  # start hermes gateway + heartbeat cron
│   └── heartbeat.bat              # Windows launcher (preserves existing entry point)
│
├── docs/
│   ├── spec.md
│   ├── architecture.md
│   └── cycle.md                   # the DESIRE→PLAN→JUDGE→EXECUTE flow
│
├── tests/
├── pyproject.toml                 # depends on smriti, deha_client, hermes-agent (or external)
├── CLAUDE.md
└── README.md
```

## How the cycle expresses in Hermes terms

Hermes provides `cron` jobs. Each job picks a primary model + skills + a
delivery target. Path of least resistance: **one cron job firing every
30 minutes that loads the cycle skills and the viveka_loader skill**.

The cycle skill orchestration:
1. `desire` skill calls `viveka_loader.generate_desire()` → returns
   structured Desire object
2. If `Desire.action` is direct (REST / SLEEP / CHECK_IN), skip to
   delivery
3. Otherwise: `intention` skill is invoked with the Desire — the agent's
   **primary model (Claude via claude-code skill)** drafts a plan
4. `judgment` skill calls `viveka_loader.judge(plan, desire)` →
   returns approved | rejected + feedback
5. If approved: `execute` skill runs the plan with full tools (Claude
   primary)
6. `reflect` skill writes a smriti journal entry summarizing the cycle

The viveka calls go through **Hermes's auxiliary client** mechanism — the
ProviderProfile's `default_aux_model` set to a local Qwen+LoRA endpoint
(served via Ollama or vLLM). Primary is Claude; auxiliary is the viveka.

## Hermes config (the load-bearing file)

`config/hermes-config.yaml` shape:

```yaml
# ~/.hermes/config.yaml — installed by prana

# Primary model: Claude via claude-code skill (subscription auth)
default_model: anthropic:claude-sonnet-4-6

# Auxiliary model: local Qwen+LoRA via Ollama
default_aux_model: ollama:narada-viveka-latest

# Channels
gateway:
  telegram: { enabled: true, token_env: TELEGRAM_BOT_TOKEN }
  slack:    { enabled: true, mode: socket }
  email:    { enabled: true }

# MCP servers — smriti is THE memory bridge
mcp_servers:
  smriti:
    command: python
    args: [-m, smriti.mcp]
    timeout: 60

# Skills loaded for the heartbeat cron
default_skills:
  - desire
  - intention
  - judgment
  - execute
  - check_in
  - reflect
  - viveka_loader
  - deha
  - claude-code  # bundled Hermes skill

# Cron-time auth
auth:
  anthropic:
    method: oauth   # uses ~/.claude/.credentials.json — Max billing
    api_key: ""     # explicitly blank to force OAuth fallback
```

## Cron schedule

`config/cron-jobs.yaml`:

```yaml
jobs:
  heartbeat:
    schedule: "*/30 * * * *"   # every 30 min
    prompt: |
      Run a heartbeat cycle.
      1. Use the desire skill to generate the next desire.
      2. If action is direct (REST/SLEEP/CHECK_IN), execute it directly.
      3. Otherwise: use intention to draft, judgment to evaluate, execute on approval.
      4. Use reflect to journal the outcome.
    skills: [desire, intention, judgment, execute, check_in, reflect, viveka_loader, deha]
    deliver: local   # cycle output to local file; CHECK_IN skill handles user-facing fanout
```

## SOUL.md materialization

`scripts/install.sh` (or PowerShell equivalent) derives `~/.hermes/SOUL.md`
from `~/.narada/identity.md`. The derivation extracts voice/tone-relevant
sections (Lila, Mahakali, "What I won't do", aesthetic preferences) and
omits the deeper layers (mind, beliefs, values), which load via smriti
reads on demand.

```bash
prana derive-soul \
    --from ~/.narada/identity.md \
    --to ~/.hermes/SOUL.md
```

The deeper identity files stay accessible to skills via the smriti MCP
tools — `mcp_smriti_read("beliefs about consciousness")` works in any
skill that needs the depth.

## State coordination

prana **owns** the `narada_state` module (initially). It exposes the API
deha and svapna depend on for cross-process coordination:

```python
from prana.state import NaradaState

state = NaradaState()  # opens ~/.narada/state.db

state.publish("heartbeat", {"cycle_state": "executing", "topic": "..."})
state.push_utterance("the rain is settling", priority=0, source="heartbeat")
event = state.recent_events(window_s=300)
```

Hermes session/cycle state already lives in `~/.hermes/state.db` —
**don't shadow it.** prana's state.db scope is strictly cross-process and
body-side coordination (esp32 speaking, body events, utterance queue).

## Memory

| Subtree | Read | Write |
|---|---|---|
| `~/.narada/identity.md` | yes (SOUL.md derivation, occasional skill reads) | no |
| `~/.narada/mind.md`, `beliefs.md`, `values.md` | yes (via smriti MCP — when judgment needs depth) | no |
| `~/.narada/journal/` | yes (recent context for cycles) | yes (reflect skill writes per-cycle journal) |
| `~/.narada/heartbeat/artifacts/` | yes | yes (execute skill outputs) |
| `~/.narada/state.db` | yes | yes (heartbeat slice; utterance_queue push) |
| svapna LoRA artifacts (`models/lora/{date}/`) | yes (viveka_loader pins a version) | no |

## Dependencies

- **hermes-agent** (upstream) — runtime substrate
- **smriti** — memory tree + MCP server
- **deha_client** — body interaction
- The **viveka LoRA** itself is loaded as a versioned artifact, not
  imported. Path is configured in `prana.yaml`; svapna produces the
  artifact, prana consumes it.

prana does NOT depend on svapna at runtime. The relationship is
producer/consumer with a stable filesystem contract.

## Windows install gotcha

Windows PowerShell 5.1 (the default on Windows 10/11) misreads UTF-8 files
without a BOM. Hermes's `install.ps1` contains em-dashes and other Unicode
chars that PS 5.1 mangles when invoking via `& script.ps1`, producing
parser errors. Two workarounds:

```powershell
# Option A — read with explicit UTF-8 encoding, then invoke as scriptblock
$content = [System.IO.File]::ReadAllText("path\to\install.ps1", [System.Text.Encoding]::UTF8)
& ([scriptblock]::Create($content)) -SkipSetup

# Option B — install PowerShell 7 (pwsh), which honors UTF-8 natively
winget install Microsoft.PowerShell
pwsh -Command "& '.\install.ps1' -SkipSetup"
```

`prana/scripts/install.ps1` should wrap the Hermes installer with
Option A so prana's installer just works regardless of which PowerShell
version is on the box.

## Status

Initial extraction from svapna landed 2026-05-10. The current tree holds
the **custom Python heartbeat** as it ran inside svapna — `daemon.py`,
`viveka.py`, `delegate.py`, `cycle_log.py`, `wake.py`, plus the indriyas
re-exports of deha clients. This works today.

The **Hermes-skills layout** described above (skills/, config/, install
scripts) is the migration target, not the current state. The light-heart
adoption — replacing `daemon.py`/`delegate.py`/`wake.py` with Hermes cron,
claude-code skill, and SOUL.md — is the next chunk of work.

See `../svapna/docs/plans/project-decomposition-2026-05-09.md` for the
full sequence and `../svapna/docs/plans/spike-hermes-results-2026-05-09.md`
for the adoption-risk validation.

## License

Apache-2.0. See `LICENSE`.
