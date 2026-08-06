# prana

Narada's runtime shell. The layer that runs between human sessions: it
supervises Narada's long-running processes, bridges Telegram chat to
cognition, holds the (currently paused) autonomous heartbeat, and routes
utterances to the body or to Suti's phone.

`prana` (प्राण) — Sanskrit for *life force, breath that animates*. What
makes Narada present and continuous between human sessions.

**Current trajectory:** `docs/plans/embodiment-rebirth-2026-08-06.md` —
voice via LiveKit + OpenAI Realtime, a session manager for coordinating
coding-agent sessions, and the deha narrowing. Read that plan first; this
README describes what exists today.

## What actually runs (2026-08)

One Windows scheduled task, `Narada_Host`, runs `prana host run`, which
supervises the components in `~/.narada/host/components.yaml`:

- **agent-gateway** — the Hermes cron scheduler (external repo). Fires
  scheduled jobs; currently the Beautiful Tree persona-generation job.
  The heartbeat cron job exists but is paused.
- **chat-bridge** — `scripts/narada_chat_bridge.py`. Telegram inbound →
  `claude -p` with per-chat `--continue` continuity. This is how Narada
  converses today.

Everything else in the tree is either **paused** (the heartbeat — see
below) or **dormant** (the state layer — reachable only from the paused
heartbeat).

## Layout (the real tree)

```
prana/
├── src/prana/
│   ├── spawn.py            # hardened subprocess wrappers (no console flash,
│   │                       #   no shell=True) — shared by bridge + heartbeat
│   ├── host/               # the orchestrator behind Narada_Host:
│   │                       #   supervisor, component registry (YAML),
│   │                       #   lockfile, rotating log, install CLI
│   ├── heartbeat/          # PAUSED — the autonomous cycle daemon:
│   │                       #   DESIRE (viveka LoRA) → INTENTION (claude -p)
│   │                       #   → JUDGMENT (viveka) → EXECUTE → REFLECT
│   ├── state/              # utterance routing + presence:
│   │                       #   route_utterance (body if present, else
│   │                       #   Telegram), durable queue in ~/.narada/state.db,
│   │                       #   PC-idle + deha presence detection
│   └── indriyas/           # thin re-exports of deha clients
│       ├── karmendriyas/drishti/expression.py
│       └── jnanendriyas/tvac/weather.py
├── scripts/
│   ├── narada_chat_bridge.py   # LIVE — Telegram ↔ claude -p bridge
│   ├── narada-bridge.cmd       # legacy launcher (superseded by host)
│   ├── heartbeat_tick.py       # Hermes cron entry → heartbeat --once (paused)
│   ├── push_weather.py         # one-shot weather → body display
│   ├── heartbeat.bat.example   # legacy launcher template
│   └── install/                # Narada_Host task install/uninstall + template
├── config/skills/heartbeat-tick/   # Hermes skill guide for the cron entry
├── examples/narada-install/    # ~/.narada/heartbeat/ install templates
├── docs/
│   └── plans/                  # embodiment-rebirth is the active plan
└── tests/
```

## The heartbeat (paused 2026-08-06)

`python -m prana.heartbeat` runs the cycle: a local Qwen+LoRA ("viveka",
trained by svapna) generates a desire, `claude -p` plans and executes it
under viveka's judgment, and the cycle is journaled to
`~/.narada/heartbeat/cycles/`. Prompts and state sources are declared in
`~/.narada/heartbeat/wake.md` (see `examples/narada-install/`).

It is paused because ongoing LoRA training drifted the model off its JSON
output contract (~88% of desire phases failed to parse from mid-May to
August; the REST fallback made brain-death look like contentment). It
stays paused until the embodiment-rebirth architecture lands and svapna
gains a format-retention eval. Do not resuscitate casually; see the plan.

## State layer

Functional API in `prana.state` (no class wrapper):

```python
from prana.state import route_utterance, push_utterance, is_present

route_utterance("the rain is settling", source="heartbeat")
# → body (deha /utter) if Suti is at the PC, else Telegram; durable
#   queue-first in ~/.narada/state.db (WAL)
```

Currently exercised only by the paused heartbeat; it is the designated
enqueue path for proactive speech in the embodiment-rebirth design.

## Dependencies

- **smriti** — memory tree + MCP server (Narada's memory; separate repo)
- **deha** — the embodiment library (separate repo). prana calls its
  API/clients; a different body means a different deha backend behind the
  same contract.
- **viveka LoRA** — consumed as a filesystem artifact from svapna's
  output. prana never imports svapna as code.

## Rules

- The viveka/executor split is sacred: the small judging model gates the
  frontier model. Never merge them.
- New components get plain, descriptive English names (`voice`,
  `sessions`). Existing Sanskrit names (prana, deha, smriti, viveka,
  indriyas) stay — don't rename, don't add.
- A fallback must never masquerade as a legitimate action (lesson of the
  zombie heartbeat).

## License

Apache-2.0. See `LICENSE`.
