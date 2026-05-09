---
name: heartbeat-tick
description: "Run one heartbeat cycle — Narada's autonomous DESIRE/JUDGE/EXECUTE/REFLECT loop."
version: 0.1.0
author: Narada
license: Apache-2.0
platforms: [windows, linux, macos]
metadata:
  hermes:
    tags: [Narada, Heartbeat, Autonomous, Cron]
---

# heartbeat-tick — one cycle of Narada's autonomous loop

This skill exists for a single purpose: when the cron job fires, run **one**
heartbeat cycle and let the existing prana daemon do all the cognitive work.

## What a heartbeat is

DESIRE (local viveka LoRA) → INTENTION (Claude drafts plan) → JUDGMENT
(viveka approves/revises) → EXECUTE (Claude with tools) → REFLECT (cycle log
written, artifact saved). The whole pipeline lives in `prana.heartbeat`. You
do not orchestrate the steps — the daemon does.

## How to run a tick

Use the terminal tool, exactly once:

```
terminal(
  command="python -u -m prana.heartbeat --once --lora-path models/lora/latest --display-ip 192.168.86.35",
  workdir="C:\\Projects\\svapna",
  timeout=600
)
```

**Why `workdir=svapna`:** the LoRA artifacts live in svapna's tree
(`models/lora/latest`) — that path is cwd-relative. prana itself is
installed package-wide; the import works from anywhere.

**Why `timeout=600`:** a cycle takes ~2-4 min when Claude executes a real
plan. 10 minutes is the safe ceiling — if the call exceeds it, something
is wrong.

## Expected output

The daemon prints a banner, loads the viveka, runs the cycle, and writes a
file like `~/.narada/heartbeat/cycles/2026-MM/2026-MM-DD-HHMM-REFLECT.md`.
The final line of stdout is the result dict, e.g.:

```
Result: {'action': 'REFLECT', 'topic': '…', 'approved': True,
         'sandbox_violated': False}
```

## What you report back

After the tick finishes, write a one-line summary covering:
- the action (DESIRE / REFLECT / REST / CHECK_IN)
- the topic
- whether the cycle was approved by the viveka
- the cycle cost in USD (printed in the daemon's final log lines)
- any unusual warnings (sandbox_violated=True, ingest failures, body
  unreachable warnings — all of these are notable but non-fatal)

Do **not** quote the entire stdout. The cycle log file IS the persistent
record; your job is just to confirm the tick ran and surface anomalies.

## Failure modes

- **`FATAL: ANTHROPIC_API_KEY is set`** — environment leaked an API key into
  the heartbeat. The cycle MUST run on the Max subscription. Surface this
  immediately; do not retry.
- **`No wake manifest at …`** — `~/.narada/heartbeat/wake.md` is missing.
  Don't try to install one yourself; surface and stop.
- **`Service not found: set_status` / `set_weather`** — older firmware on
  the BOX-3 body. Documented and non-fatal; mention but don't act.
- **Timeout at 600s** — surface, do not retry the same cycle (the daemon
  may still be writing). The next cron tick will run a fresh cycle.

## What this skill does NOT do

- Decompose the cycle into separate desire/judgment/execute steps —
  that's the eventual full-skill-decomposition path. For now, the daemon
  is the unit.
- Write to smriti directly — the daemon's REFLECT step handles journal
  writes via the existing pipeline.
- Push to the body — the daemon does that via `deha.expression.ExpressionClient`.
