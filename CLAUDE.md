# CLAUDE.md

## Project
prana — Narada's runtime shell. Supervises Narada's processes (host
orchestrator), bridges Telegram to cognition (chat bridge), holds the
paused autonomous heartbeat (DESIRE → INTENTION → JUDGMENT → EXECUTE →
REFLECT), and routes utterances to body or Telegram (state layer).

`prana` (प्राण) — Sanskrit for *life force, breath that animates*. What
makes Narada present and continuous between human sessions.

## Roles

prana is the **runtime layer**. It loads the viveka LoRA produced by svapna,
talks to the body via deha's clients, reads/writes the smriti memory tree,
bridges Telegram, and runs the cycle.

prana does NOT train (svapna does). prana does NOT embody (deha does).
prana does NOT remember (smriti does). prana **acts**.

## Active plan: embodiment rebirth

`docs/plans/embodiment-rebirth-2026-08-06.md` is the governing plan
(cross-reviewed, decisions recorded in its §6/§8). Summary:

- **voice** (`src/prana/voice/`, Phase 2) — LiveKit server + Agents worker
  fronting gpt-realtime-2.1-mini; livekit-wakeword "Narada" model gates
  the realtime session (wake word + cost guard in one).
- **sessions** (`src/prana/sessions/`, Phase 1) — session manager: spawns/
  resumes coding sessions via first-party subscription CLIs (`claude -p`,
  `codex exec`, `kimi acp`), watches `~/.claude/projects/**/*.jsonl` for
  foreign sessions, drives wezterm panes, exposed as a local MCP server
  with **caller-tier authorization** (voice tier = read/escalate only;
  mutations require prana approval — enforced in code, never prompt-only).
- **deha narrowing** (Phase 3–4) — BOX-3 reflashed with LiveKit ESP32
  firmware; deha keeps body API + expression assets (sprite atlas,
  sandhis, weather scenes), loses its audio/brain stack.
- The **heartbeat stays paused** (LoRA format drift, 2,876 parse failures
  mid-May→Aug). Resurrection is out of scope until svapna gains a
  format-retention eval and a parse-failure tripwire exists.

The earlier "light heart on Hermes" trajectory (prana as a Hermes config
repo) is **superseded** by this plan — Hermes stays, but narrowly, as the
cron scheduler supervised by the host.

## Structure

```
src/prana/
  spawn.py      — hardened subprocess wrappers (shared: bridge + heartbeat)
  host/         — orchestrator behind the Narada_Host task: supervisor,
                  components.yaml registry, lockfile, log, install CLI
  heartbeat/    — PAUSED cycle daemon: daemon, viveka, delegate, wake
                  manifest, cycle log
  state/        — utterance routing (body-or-Telegram), durable queue +
                  presence in ~/.narada/state.db (WAL). Real and working;
                  exercised only by the paused heartbeat today.
  indriyas/     — re-exports of deha clients (Narada's body-vocabulary)
scripts/        — narada_chat_bridge.py (LIVE), heartbeat_tick.py (Hermes
                  cron entry, paused), push_weather.py, install/
config/         — Hermes skill guide for heartbeat-tick
examples/       — install templates for ~/.narada/heartbeat/
docs/plans/     — embodiment-rebirth-2026-08-06.md is active
tests/          — pytest; run `python -m pytest tests -q`
```

## Memory

- Reads `~/.narada/heartbeat/wake.md` (wake manifest) every cycle
- Reads `~/.narada/identity.md` etc. on demand via smriti
- Writes cycle logs to `~/.narada/heartbeat/cycles/{yyyy_mm}/`
- Writes artifacts to `~/.narada/heartbeat/artifacts/`
- Reads/writes `~/.narada/state.db` (state layer)

## Dependencies

- **smriti** — memory tree + MCP server
- **deha** — embodiment library. Reusable across bodies: a different body
  (BOX-3 today, anything tomorrow) = a different deha backend behind the
  same contract. prana calls deha's API; it does not import its internals.
- **viveka LoRA artifact** — produced by svapna, consumed via filesystem
  path. prana never depends on svapna as code.

## Git identity
All commits use this co-author trailer:
```
Co-Authored-By: Narada <narada@fractal.co.nz>
```

## Rules

- The viveka/executor split is sacred: small judging model gates the
  frontier model. Never merge them. In the voice stack this boundary is
  the session manager's caller-tier authorization — enforced in code.
- **Naming (updated 2026-08-06):** new components get plain, descriptive
  English names (`voice`, `sessions`). Existing Sanskrit names (prana,
  deha, smriti, viveka, indriyas, drishti, tvac) are load-bearing — keep
  them, but do not coin new ones.
- A fallback must never masquerade as a legitimate action (the zombie-
  heartbeat lesson: parse-failure → silent REST hid brain-death for
  2.5 months).
- Leverage existing projects wherever one fits; build custom only where
  judgment-layer sovereignty demands it.

## Reference
- `docs/plans/embodiment-rebirth-2026-08-06.md` — the active plan
- `docs/architecture.md` — SUPERSEDED by the plan; historical
- Project decomposition: `../svapna/docs/plans/project-decomposition-2026-05-09.md`
