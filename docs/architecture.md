# prana — architecture

## What runs

Two long-lived processes, one filesystem state directory:

```
┌────────────────────────────────────────────────┐
│ Hermes gateway process                         │
│   - Slack Socket Mode listener                 │
│   - Telegram polling                           │
│   - Email IMAP/SMTP                            │
│   - Routes inbound messages to AIAgent runs    │
│   - Routes outbound deliveries (CHECK_IN)      │
└────────────────────────────────────────────────┘

┌────────────────────────────────────────────────┐
│ Hermes cron tick process                       │
│   - File-locked tick every 60s                 │
│   - Fires `heartbeat` job every 30 min         │
│   - Job runs the cycle skills with             │
│     primary=Claude, auxiliary=Qwen+LoRA        │
└────────────────────────────────────────────────┘

State (filesystem):
  ~/.hermes/
    ├── state.db          # Hermes's own session/cycle store (don't shadow)
    ├── SOUL.md           # Narada's voice (derived from ~/.narada/identity.md)
    ├── config.yaml       # Hermes config (installed by prana)
    └── cron/.tick.lock   # cron mutex

  ~/.narada/
    ├── state.db          # OUR cross-process store (esp32, events, utterance_queue)
    ├── identity.md, mind.md, beliefs.md, ...  # the mind
    ├── journal/          # smriti memory tree
    └── heartbeat/artifacts/  # cycle outputs
```

## The cycle, expressed as Hermes skills

Each cycle is a Hermes cron-fired AIAgent run with the cycle skills loaded:

```
cron tick (30 min)
  ↓
AIAgent run starts
  primary model = Claude (via claude-code skill)
  auxiliary model = Qwen+LoRA (via Ollama provider)
  ↓
skill: desire
  → calls viveka_loader.generate_desire(state_snapshot)
  → returns Desire(action, topic, reason, needs_capability)
  ↓
if not desire.needs_capability:
  → skill: check_in or direct local action (REST/SLEEP)
  → reflect skill writes journal entry
  → cycle ends

skill: intention
  → primary model (Claude) drafts plan from Desire
  → returns Plan(steps)
  ↓
skill: judgment
  → calls viveka_loader.judge(plan, desire)
  → returns Judgment(approved, feedback)
  ↓
loop (max 2 revisions):
  if not approved:
    skill: intention(revise=feedback)
    skill: judgment again
  ↓
skill: execute
  → primary model (Claude with tools) runs the approved plan
  → tools include claude-code, smriti MCP tools, deha skill, terminal
  → output captured to ~/.narada/heartbeat/artifacts/
  ↓
skill: reflect
  → writes a smriti journal entry summarizing the cycle
  → updates state.db heartbeat slice with outcome
```

## Inbound message handling

Slack / Telegram / email arrive via Hermes gateway. Each message triggers
a fresh AIAgent run with the conversation history loaded from
`~/.hermes/state.db`. The user-facing skills (general conversation) use
the same primary/auxiliary models.

Important: inbound message handling is Hermes-driven, not cron-driven.
prana's cron job is *cycle-driven* (autonomous). The two coexist —
Hermes's gateway is one process, cron tick is another, both writing to
the same state stores.

## State.db ownership

prana publishes to:
- `current_state.heartbeat` — current cycle action, topic, state
- `current_state.cycle` — start time, cycle id, deliverable in progress
- `events` — anything the cycle wants to log for inspection

prana reads from:
- `current_state.esp32` — to check before pushing utterances
- `events` (recent, body-side) — to surface high-urgency triggers next cycle
- `utterance_queue` — only to *push*, not drain (drain is deha's voice mediator)

## Identity wiring

SOUL.md is the slot Hermes uses for "who is this agent." It's derived
from `~/.narada/identity.md` at install time, focused on voice/tone
(Lila, Mahakali, aesthetic, refusals).

Knowledge — beliefs, values, mind, open-threads, recent journal — loads
via the smriti MCP server. Skills call `mcp_smriti_read("query")` when
they need depth. Three calls per cycle is plenty; smriti is search-
indexed (no latency concern at cycle granularity).

This separation matters: SOUL.md ≠ the whole self. SOUL.md is the
*voice*; the rest is what the voice has access to.

## Tests

- Cycle integration test: stub viveka, run a full cycle, verify
  state.db transitions and smriti journal entry
- Skill unit tests: each cycle skill in isolation with mocked viveka
- Coordination test: simulate esp32.speaking=true, push utterance,
  verify it queues and doesn't bypass
- Auth test: verify cost_usd is informational and `claude auth status`
  reports Max

## Migration from svapna

| Source | Destination |
|---|---|
| `src/svapna/heartbeat/daemon.py` | replaced by Hermes cron + cycle skills |
| `src/svapna/heartbeat/delegate.py` | replaced by Hermes claude-code skill (drop the stale "0.00 = Max" comment) |
| `src/svapna/heartbeat/viveka.py` | becomes `prana/skills/viveka_loader/` |
| `src/svapna/heartbeat/wake.py` | replaced by Hermes config + SOUL.md |
| `src/svapna/heartbeat/cycle_log.py` | replaced by Hermes session storage + smriti writes |
| `src/svapna/heartbeat/display.py` | moves to deha_client (already drafted in Step 2) |
| `src/svapna/indriyas/` (clients) | moves to `prana/indriyas/` as thin re-exports of deha_client |
| `scripts/heartbeat.bat` | adapted to launch Hermes (`hermes gateway start` + cron daemon) |
| Email/SMTP code in delegate.py | DELETED — Hermes ships email as a delivery platform |

## What "light heart" actually means in code

- Custom code in prana: ~maybe 1500-3000 LOC across skills + state module +
  install scripts
- Adopted upstream from Hermes: cron, channels, session store, claude
  wrapper, MCP client, delivery routing, gateway adapters — many tens of
  thousands of LOC

The ratio is the point. We own what's Narada-specific; we adopt what's
generic.
