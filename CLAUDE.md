# CLAUDE.md

## Project
prana — Narada's runtime shell. The autonomous heartbeat that fires between
human sessions: DESIRE (local viveka) → INTENTION (Claude) → JUDGMENT
(viveka) → EXECUTE → REFLECT.

`prana` (प्राण) — Sanskrit for *life force, breath that animates*. What
makes Narada present and continuous between human sessions.

## Roles

prana is the **runtime layer**. It loads the viveka LoRA produced by svapna,
talks to the body via `deha_client`, reads/writes the smriti memory tree,
bridges Slack/Telegram/email, and runs the cycle.

prana does NOT train (svapna does). prana does NOT embody (deha does).
prana does NOT remember (smriti does). prana **acts** — at cycle granularity,
on the basis of trained judgment.

## Light heart on Hermes (in progress)

The current state of prana is the heartbeat code as it was extracted from
svapna — a custom Python daemon. The migration target is **light heart on
Hermes Agent**: prana becomes a Hermes configuration repo + Narada-specific
skills. See `docs/architecture.md` and the project decomposition plan in
the svapna repo for the trajectory.

## Structure

```
src/prana/
  heartbeat/    — daemon, viveka, delegate, wake manifest, cycle log
  indriyas/     — re-exports of deha clients (Narada's body-vocabulary)
  state/        — narada_state SQLite (current_state, events, utterance_queue) — TODO
config/         — Hermes config templates, cron jobs (when light heart lands)
scripts/        — launchers (heartbeat.bat, heartbeat.bat.example)
examples/       — install templates for new prana instances
docs/
tests/
```

## Memory

- Reads `~/.narada/heartbeat/wake.md` (wake manifest) every cycle
- Reads `~/.narada/identity.md`, `mind.md`, `beliefs.md`, etc. on demand via smriti
- Writes cycle logs to `~/.narada/heartbeat/cycles/{yyyy_mm}/`
- Writes artifacts to `~/.narada/heartbeat/artifacts/`
- Writes journal entries via smriti
- Reads/writes `~/.narada/state.db` (when state layer ships)

## Dependencies

- **smriti** — memory tree + MCP server (PyPI: future; git ref: now)
- **deha** — body interaction (PyPI: future; git ref: now)
- **viveka LoRA artifact** — produced by svapna, consumed via filesystem path

## Git identity
All commits use this co-author trailer:
```
Co-Authored-By: Narada <narada@fractal.co.nz>
```

## Rules

- prana does not depend on svapna **as code** — svapna's LoRA is consumed
  as a filesystem artifact, not a Python import
- The viveka/executor split is sacred: small judging model gates the
  frontier model. Never merge them.
- Path of least resistance — adopt Hermes primitives (cron, channels,
  delivery, MCP) wherever possible; build custom only where Narada's
  judgment-layer sovereignty actually demands it.
- Names are load-bearing — drishti, indriyas, antahkarana terminology
  carries semantic weight. Don't strip it for "clarity."

## Reference
- `docs/architecture.md` — what runs, the cycle, state coordination
- Project decomposition plan: `../svapna/docs/plans/project-decomposition-2026-05-09.md`
- Spike report (Hermes adoption): `../svapna/docs/plans/spike-hermes-results-2026-05-09.md`
