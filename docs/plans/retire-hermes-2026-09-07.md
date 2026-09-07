# Retire Hermes — Windows Scheduler becomes the scheduler singleton

**Date:** 2026-09-07 (revised after cross-review round 1)
**Status:** PROPOSED — round 1 complete (needs-attention, 5 high + 1
medium, ALL ACCEPTED and folded in below). Round-2 confirmation
requested before execution.
**Supersedes:** the one-process addendum's ruling that *Hermes* is the
scheduler singleton (personal-agent-platform-2026-09-04.md, Addendum
§"five small singletons", item 4). This plan makes **Windows Task
Scheduler** the scheduler singleton and removes Hermes from the runtime.

## Why

Audit of the live Hermes cron store (2026-09-07): Hermes runs exactly
three active jobs, plus a paused one:

| Hermes job | schedule | what it is |
|---|---|---|
| `bt-figures-populate` | 6×/day (`17 21,23,1,3,5,7 * * *`) | shim → detached BT figure-dossier gen (Codex), returns immediately |
| `akhada-checkin` | 3×/day (`0 11,18,22 * * *`) | shim → `prana/scripts/akhada_checkin_tick.py` (has a dedupe record) |
| `daily-debrief` | daily 18:30 | shim → `prana/scripts/daily_debrief.py` (runs `claude -p`, DELIVERS a message via the state router) |
| `narada-heartbeat` | (paused since 2026-08-06) | not running; resurrection out of scope |

Every active job is a `no_agent` `--script` shim that shells out to a
prana-repo script, all `deliver=local` (stdout → log). None use Hermes's
agent, orchestration, or Telegram delivery. Hermes contributes only the
schedule trigger and a log. The Telegram chat bridge is a **separate**
component (`narada_chat_bridge.py`), not Hermes.

So Hermes can be replaced by Windows tasks calling the prana scripts
directly (also deleting the shim layer): one fewer supervised component,
one fewer subscription-framework process, one fewer copy-kept script
dir. **But** the round-1 review established that the naïve version of
this is unsafe; the design below is the corrected one.

## Governing constraints (unchanged from the addendum, sharpened here)

- **Never silent**, and **the watchdog must be outside the watched.**
  Round-1 finding 2 exposed that this plan's observer (job-watch) is
  *itself a Windows scheduled task* — so a Task Scheduler **service**
  failure would stop the three jobs AND their overdue checks together.
  Windows Scheduler being outside the job *processes* is not the same as
  being outside their *scheduling dependency*. This gap **already exists
  today** (the deadman is already a Windows task); retiring Hermes only
  widens its blast radius. Therefore this plan now REQUIRES a
  scheduler-independent observer (§Scheduler observer) as a prerequisite,
  not an afterthought.
- **The brain never schedules.** These jobs do not move "into the
  brain"; they are deterministic ticks. A scheduler inside cognition is
  the coupling the addendum exists to avoid.
- **Pause-first, drain, then enable** (round-1 finding 1). Reversed from
  the first draft: delivery jobs can double-fire during an overlap, so
  the old trigger is paused and in-flight work drained BEFORE the new
  task is enabled. Rollback reverses (pause new, enable old).
- **Blocking failure-path acceptance per task** (round-1 finding 5) —
  the addendum's rejected "one happy run" gate is not reinstated here.
  See §Acceptance gates.

## Scheduler observer (round-1 finding 2 — prerequisite)

The prana **host** is a long-running process: Task Scheduler launches it
at logon, but once running its liveness does NOT depend on the Scheduler
service. So the host is the natural scheduler-independent observer.

- The host gains a periodic check (in its existing event loop, alongside
  the heartbeat write) that **`Narada_Deadman` has run within the last
  ~15 min** — read from a durable deadman progress file the deadman
  writes at the end of each tick (`%LOCALAPPDATA%/narada/deadman.progress`
  with a UTC timestamp), not from Task Scheduler APIs (which may be the
  thing that's down). If stale, the host pages: "Task Scheduler / deadman
  is not firing."
- This closes the loop into **mutual, cross-domain watching**: the
  deadman (a scheduled task) watches the host *process*; the host
  *process* watches the deadman's *scheduling*. A single failure on
  either side is now visible from the other. Both would have to fail
  simultaneously to go silent.
- **Pre-retirement drill (blocking):** stop the Task Scheduler service,
  confirm the host detects deadman-staleness and pages, restart the
  service. Hermes is not retired until this passes.

(The deadman writing `deadman.progress` and the host reading it is a
small addition to already-live code; it is a prerequisite of this plan
and lands + is diff-reviewed before any job is cut over.)

## Target design

Three Windows scheduled tasks, each invoking the prana script directly
through the existing `run-hidden.vbs` shim (no console flash), each with
**explicit multiple triggers** (Windows tasks support N triggers per
task — round-1 next-step) rather than a cron expression:

| Task | triggers | action (via run-hidden.vbs) | check mode | retry |
|---|---|---|---|---|
| `Narada_Figures` | 6 daily: 21:17, 23:17, 01:17, 03:17, 05:17, 07:17 | `python prana/scripts/populate_figures_tick.py`* | **result+overdue** | none |
| `Narada_Akhada_Checkin` | 3 daily: 11:00, 18:00, 22:00 | `python prana/scripts/akhada_checkin_tick.py` | result+overdue | none |
| `Narada_Daily_Debrief` | 1 daily: 18:30 | `python prana/scripts/daily_debrief.py` | result+overdue | none |

\* moved into the prana repo first (see step 1); currently only in
`~/.hermes/scripts` + an AppData copy.

**Timezone/DST (round-1 next-step):** the live Hermes cron runs in
host-local time (Australia/Brisbane, which does NOT observe DST, so
there is no spring/autumn skip-or-repeat risk here). Windows triggers
default to host-local; the tasks are registered with local
`StartBoundary` values matching the times above. The absence of DST in
the host timezone is recorded so a future timezone change re-opens this.

**Failover settings** (the robustness the current smriti tasks lack):
`StartWhenAvailable=true` (replay a run missed while the box was off —
see the replay policy below), `ExecutionTimeLimit` per job (figures
returns fast after a detached launch → short; debrief ≤15 min; check-in
≤2 min), `MultipleInstances=IgnoreNew`.

**No automatic RestartOnFailure** (round-1 finding 1). A crash-retry
risks redelivery for the delivery jobs (debrief) and a duplicate 40-min
generation for figures. A failed run is instead caught by job-watch
(result+overdue) and re-attempted on the next scheduled trigger.
Enabling retry later requires durable per-occurrence dedup first
(§Deferred).

**check-mode = result+overdue for ALL THREE** (round-1 finding 3
corrected my earlier overdue-only for figures): a nonzero *launcher*
exit is real signal (the tick failed to even start its work) and must
page. What the launcher exit CANNOT capture is the outcome of figures'
*detached* generation — that is a separate, explicitly-out-of-scope gap
(§Deferred), not a reason to ignore launch failures.

**Missed-occurrence replay policy (round-1 finding 5):** per job —
- figures: skip (a missed 3am slot is not worth a catch-up 40-min gen;
  the next slot covers it). `StartWhenAvailable=false` for figures.
- check-in / debrief: replay at most the most-recent missed run
  (`StartWhenAvailable=true`), because a debrief/check-in the operator
  should have received is worth delivering late; older missed runs are
  not replayed (Windows runs only the single most-recent missed
  occurrence, which matches this intent).

## Logging & outcome records (round-1 finding 6)

Hermes gave each job stdout→a local log. The replacement must not lose
that:
- Each task's action redirects the prana script's stdout+stderr to a
  durable per-job log under `%LOCALAPPDATA%/narada/logs/cron/<job>.log`
  (the `run-hidden.vbs` shim is extended to take a log path, or a tiny
  per-job wrapper does the redirect), capped/rotated like the deadman log.
- `LastTaskResult` + `LastRunTime` are the per-occurrence outcome record
  (job-watch already reads them).
- **Figures' detached child:** before cutover, verify where the detached
  generation's output currently goes (Hermes captured only the tick's
  immediate stdout, NOT the detached child's later output — so this is
  not a regression, but it must be confirmed and the child's actual
  output destination documented). Closing the detached-completion
  blind spot (a completion/heartbeat file the generation writes, watched
  separately) is §Deferred.

## Migration steps (each independently reversible)

0. **Snapshot for rollback (round-1 finding 4) — FIRST.** Copy, into
   `prana/docs/archive/hermes-2026-09-07/`: the full `~/.hermes/cron/
   jobs.json`, all four referenced scripts (both figures copies
   included), the `agent-gateway` component block, and the Hermes
   autostart definition. This is the executable rollback set; nothing is
   deleted until §cleanup, and cleanup is gated on a verified restore
   test.
1. **Build the scheduler observer** (§Scheduler observer): deadman writes
   `deadman.progress`; host checks its freshness and pages on staleness.
   Land + diff-review + pass the Task-Scheduler-stopped drill BEFORE any
   job cutover.
2. **Move `populate_figures_tick.py` into prana** (`scripts/`), commit,
   verify detached-launch + env-stripping behavior from the repo path.
3. **Register the three Windows tasks disabled** (via a new
   `install-cron-tasks.ps1` / `prana host install-cron`
   `--dry-run|--force|--enable|--uninstall`), with the triggers, logging
   redirect, and settings above.
4. **Add the three tasks to job-watch** (template + live config), all
   result+overdue.
5. **Per-task acceptance gate (blocking — round-1 finding 5).** For each
   task, before its Hermes twin is paused, verify on the real task:
   (a) a nonzero launcher exit propagates to `LastTaskResult` through the
   vbs shim (deliberately break the script once); (b) job-watch pages on
   that failure and on a simulated overdue; (c) a failed page retries;
   (d) the `ExecutionTimeLimit` terminates a hung run; (e) a
   missed-while-off run replays or skips per policy; (f) a normal run
   produces the expected side effect exactly once.
6. **Cutover per job, pause-first (round-1 finding 1):** pause the Hermes
   job, drain any in-flight run, THEN enable the Windows task. Never both
   active for the same occurrence. Watch one full day of real triggers.
7. **Retire Hermes** only after all three are stable for a full cycle
   AND the scheduler-observer drill (step 1) passed: set `agent-gateway`
   `enabled=false`, stop the gateway, and (after Suti's ok) remove the
   Hermes autostart. Verify the rollback restore from the step-0
   snapshot in a dry run BEFORE deleting the live Hermes store.
8. **Heartbeat:** the paused `narada-heartbeat` definition is already in
   the step-0 snapshot; that is its archive. Let the live store go with
   the rest.

## Rollback

Before step 7: pause the Windows task, re-enable the Hermes job
(pause-first, reversed). After step 7: restore from the step-0 snapshot
(`jobs.json` + scripts + `agent-gateway` block + autostart) — which is
why cleanup is gated on a verified restore test, not merely on
"hermes-spike stays on disk" (round-1 finding 4).

## Deferred (named, out of scope for this cutover)

- **Figures detached-completion monitoring** — the tick's exit proves it
  launched, not that the 20–40-min generation succeeded (a Hermes-era
  blind spot, unchanged here). Future: the generation writes a
  completion record; a watcher pages on a missing/failed completion.
- **Automatic crash-retry for delivery jobs** — requires durable
  per-occurrence dedup (atomic cross-process claim) before it is safe.

## Round-1 cross-review (Codex gpt-6-astra, 2026-09-07)

Verdict needs-attention; 5 high + 1 medium; ALL ACCEPTED:
1. Enable-first double-fire on delivery jobs → **pause-first + drain**,
   no auto-retry for delivery jobs, dedup deferred. (§constraints,
   §target, step 6)
2. Observer shares the scheduler's failure domain → **host-side
   scheduler observer** reading a deadman progress file, + a
   Scheduler-stopped drill. (§Scheduler observer, step 1)
3. Overdue-only hides figures launch failures → **result+overdue for
   all three**; detached-completion kept separate. (§target, §Deferred)
4. Cleanup removes rollback artifacts → **step-0 full snapshot** +
   verified restore test gating cleanup. (step 0, step 7, §Rollback)
5. Dropped the addendum's failure-path gates → **blocking per-task
   acceptance** (nonzero propagation, timeout, missed-run, overdue
   detection, failed-page retry) before retiring Hermes. (step 5)
6. Log preservation unspecified → **durable per-job logs** + outcome
   records; figures child output destination verified. (§Logging)

Next-steps also folded in: explicit multiple triggers (6/3/1);
timezone/DST recorded (host TZ has no DST); gateway-consumer inventory
before shutdown (step 7 / the audit is cron-only — a full consumer
inventory is added to step 7's precondition).
