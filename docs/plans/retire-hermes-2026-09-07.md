# Retire Hermes — Windows Scheduler becomes the scheduler singleton

**Date:** 2026-09-07
**Status:** PROPOSED — cross-review requested before execution.
**Supersedes:** the one-process addendum's ruling that *Hermes* is the
scheduler singleton (personal-agent-platform-2026-09-04.md, Addendum
§"five small singletons", item 4). This plan makes **Windows Task
Scheduler** the scheduler singleton instead and removes Hermes from the
runtime entirely.

## Why

Audit of the live Hermes cron store (2026-09-07) found Hermes is doing
exactly three things, all of them thin:

| Hermes job | schedule | what it is |
|---|---|---|
| `bt-figures-populate` | 6×/day (`17 21,23,1,3,5,7 * * *`) | shim → detached BT figure-dossier gen (Codex) |
| `akhada-checkin` | 3×/day (`0 11,18,22 * * *`) | shim → `prana/scripts/akhada_checkin_tick.py` |
| `daily-debrief` | daily 18:30 | shim → `prana/scripts/daily_debrief.py` |
| `narada-heartbeat` | (paused since 2026-08-06) | not running; resurrection out of scope |

Findings:
- **Every active job is a `no_agent` `--script` shim that shells out to
  a script in the prana repo**, and all deliver `local` (stdout → log).
  None use Hermes's agent, orchestration, or Telegram delivery. Hermes
  contributes only the schedule trigger and a log line.
- Hermes is otherwise a full Nous-Portal-subscription agent framework
  (`hermes-spike`) running as `agent-gateway` under the host — heavy
  for the role of "cron with a log."
- The Telegram chat bridge is a **separate** component
  (`narada_chat_bridge.py`), not Hermes; dropping Hermes does not touch
  chat, voice, smriti, the brain, or the wake system. Nothing but these
  three cron jobs depends on the gateway.

So Hermes can be replaced by Windows Task Scheduler calling the prana
scripts **directly** — which also deletes the shim layer. Result: one
fewer supervised component, one fewer subscription-framework process,
one fewer copy-kept script directory (`~/.hermes/scripts` +
`%LOCALAPPDATA%/hermes/scripts`), and the scheduler singleton is the OS
service that is already there, already survives reboots, and is already
watched from outside by the deadman's job-watch (step 4a).

## Governing constraints (unchanged from the addendum)

- **Never silent.** Windows Scheduler's own failover (`RestartOnFailure`,
  `StartWhenAvailable`) catches crashes and missed-while-off runs — but
  NOT a job that "succeeds" doing nothing, nor a hang, and it pages no
  one. That observability gap is filled by **job-watch** (already live),
  not by Scheduler. Therefore: no job moves to a Windows task without a
  matching job-watch config entry. Scheduler + job-watch together are
  the robust unit; Scheduler alone is not.
- **The brain never schedules.** These jobs do NOT move "into
  narada-brain": they are deterministic ticks (figures fires Codex
  detached; debrief runs its own `claude -p`/Codex; check-in runs
  akhada's decision + prana's router). A scheduler inside the cognition
  process is the coupling the addendum exists to avoid.
- **Disable, don't delete, until verified.** Each Hermes job is paused
  (not removed) and each old script copy retained until the replacement
  task has one verified successful run. Rollback is re-enabling the
  Hermes job.

## Target design

Three Windows scheduled tasks, each invoking the **prana script
directly** through the existing `run-hidden.vbs` shim (no console flash),
each configured for real failover:

| Task | schedule | action (via run-hidden.vbs) | check mode |
|---|---|---|---|
| `Narada_Figures` | `17 21,23,1,3,5,7 * * *` | `python C:\Projects\prana\scripts\populate_figures_tick.py`* | overdue-only |
| `Narada_Akhada_Checkin` | `0 11,18,22 * * *` | `python C:\Projects\prana\scripts\akhada_checkin_tick.py` | result+overdue |
| `Narada_Daily_Debrief` | `30 18 * * *` | `python C:\Projects\prana\scripts\daily_debrief.py` | result+overdue |

\* `populate_figures_tick.py` currently lives in `~/.hermes/scripts/`
with a second copy in `%LOCALAPPDATA%/hermes/scripts/`. It must first be
**moved into the prana repo** (`prana/scripts/populate_figures_tick.py`)
so it is versioned like the other two ticks; the task then points at the
repo copy and both Hermes copies are retired. (Open question below on
whether figures should move now or stay on Hermes one more cycle.)

Task settings for all three (the robustness the current smriti tasks
lack): `StartWhenAvailable=true`, `RestartOnFailure count=2
interval=PT5M`, a sane `ExecutionTimeLimit` per job (figures fires
detached and returns fast, so a short limit; debrief up to ~15 min),
`MultipleInstances=IgnoreNew`.

**check-mode rationale:** figures is fire-and-forget (the tick returns
0 after launching detached work; its exit code says nothing about the
generation) → overdue-only, so we only alarm if the *trigger* stops
firing. check-in and debrief run their real work synchronously and
return a meaningful exit code → result+overdue.

**Health caveat for figures (carry-over risk):** because the figures
tick returns 0 immediately after a detached launch, NEITHER Scheduler
NOR job-watch can see whether the actual 20–40-min generation
succeeded. This is a pre-existing blind spot (Hermes had it too), not
introduced here — but it should be named, and closing it (a
completion/heartbeat file the generation writes, watched separately) is
future work, out of scope for this cutover.

## Migration steps

1. **Move `populate_figures_tick.py` into prana** (`scripts/`), commit,
   verify it runs from the repo path with the same detached-launch +
   env-stripping behavior. (Or defer figures — see open question.)
2. **Register the three Windows tasks** (disabled), pointing at the
   prana scripts via `run-hidden.vbs`, with the failover settings above.
   A new installer `scripts/install/install-cron-tasks.ps1` (mirrors
   `install-deadman.ps1`), exposed as `prana host install-cron` with
   `--dry-run` / `--force` / `--enable` / `--uninstall`.
3. **Add the three tasks to `job-watch.template.json`** and the live
   `job-watch.json`, with the check modes above.
4. **Cutover, one job at a time:** enable the Windows task, verify one
   real run lands (figures: a detached gen starts; check-in/debrief:
   exit 0 + expected side effect), THEN pause the corresponding Hermes
   job. Windows task and Hermes job never both fire the same work — the
   enable/pause is a single-owner handoff, ordered enable-first only
   because these are idempotent-safe/low-stakes; if any job were
   mutation-heavy it would be pause-first.
5. **Retire Hermes:** once all three are handed over and stable for one
   full cycle, set `agent-gateway` `enabled=false` in `components.yaml`,
   stop the gateway, and (after Suti's ok) uninstall the Hermes
   autostart. The `hermes-spike` repo stays on disk; only its runtime
   role in Narada is removed.
6. **Archive the heartbeat definition:** copy the paused
   `narada-heartbeat` job JSON into `prana/docs/archive/` so the cron
   spec (schedule, script, --once semantics) is preserved for a future
   resurrection, then let the Hermes store go.

## Rollback

Each step is independently reversible: re-enable a Hermes job and
disable its Windows task (single-owner, either direction). Full
rollback before step 5 = re-enable all three Hermes jobs, disable the
three Windows tasks. After step 5, rollback = re-enable `agent-gateway`
and the Hermes jobs from their retained (disabled) definitions.

## Open questions for the reviewer / Suti

1. **Figures now or later?** `populate_figures_tick.py` is the one job
   not yet in the prana repo and the one with the detached-generation
   blind spot. Move it now (clean, all-in-one cutover) or leave it on
   Hermes for one more cycle and cut over only check-in + debrief first
   (smaller blast radius, but Hermes lingers for one job)?
2. **RestartOnFailure count** — 2×/5min proposed. For check-in/debrief a
   crash retry is fine; is there any job where a retry could double a
   side effect? (check-in has a dedupe record; debrief delivers via the
   state router — both look retry-safe, to be confirmed.)
3. **Heartbeat:** archive-and-drop (proposed) vs keep the Hermes store
   intact as the heartbeat's home against resurrection.
