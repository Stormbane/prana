# Retire Hermes — Windows Scheduler becomes the scheduler singleton

**Date:** 2026-09-07 (revised after cross-review round 1)
**Status:** CROSS-REVIEWED — rounds 1 (5 high + 1 medium) and 2 (3
high) both needs-attention, ALL findings accepted and folded in; plan
debate closed per protocol. Awaiting Suti's go/no-go before execution.
Implementation, if approved, gets a diff review per protocol step 6.
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

The prana **host** is a long-running process: once running, its liveness
does NOT depend on the Scheduler service. So a running host is the
natural scheduler-independent observer — but *getting it running* must
also not depend solely on Task Scheduler (round-2 finding 2).

- The host gains a periodic check (in its existing event loop, alongside
  the heartbeat write) that **`Narada_Deadman` has run within the last
  ~15 min** — read from a durable deadman progress file the deadman
  writes at the end of each tick (`%LOCALAPPDATA%/narada/deadman.progress`
  with a UTC timestamp), not from Task Scheduler APIs (which may be the
  thing that's down). If stale, the host pages: "Task Scheduler / deadman
  is not firing."
- This closes the loop into **mutual, cross-domain watching**: the
  deadman (a scheduled task) watches the host *process*; the host
  *process* watches the deadman's *scheduling*.
- **Bootstrap independence (round-2 finding 2).** If Task Scheduler
  fails to start after a reboot, it launches neither `Narada_Host` nor
  `Narada_Deadman` — no on-box observer would exist. So the host is
  **dual-bootstrapped**: the `Narada_Host` scheduled task AND a
  Startup-folder shortcut (processed by Explorer at logon, a bootstrap
  path independent of the Task Scheduler service). The host's existing
  lockfile makes a double-launch idempotent (second instance exits). A
  Scheduler-dead-at-boot therefore still brings the host up via the
  Startup folder, and the running host then observes that the deadman /
  Scheduler is not firing and pages.
- **Blocking drills (both, before retiring Hermes):**
  (a) *Scheduler-down while running* — stop the Task Scheduler service
  with the host already up; confirm the host detects deadman-staleness
  and pages.
  (b) *Scheduler-dead at boot* — disable/break Task Scheduler autostart,
  reboot (or log off/on), confirm the Startup-folder path brings the
  host up and it pages that the scheduler is not firing.
- **Out of scope (named):** if the whole box is off or wedged before
  logon, nothing on-box can page. A truly external dead-man's-switch
  (the box pings an off-box service every N min; that service pages on
  silence) is the only cover for box-fully-down and is **deferred** —
  it is independent of the Hermes question and belongs to its own plan.

(The deadman writing `deadman.progress`, the host reading it, and the
Startup-folder dual-bootstrap are small additions to already-live code;
they are a prerequisite of this plan and land + are diff-reviewed before
any job is cut over.)

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

\* moved into the prana repo first (see step 2); currently only in
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
5. **Per-job cutover — pause-first, THEN accept, THEN enable (round-1
   finding 1 + round-2 finding 1).** The single-owner rule applies to
   the acceptance phase too: a production-side-effect test on the Windows
   task while Hermes is still active would double-fire (daily-debrief
   delivers on every invocation, no occurrence dedup). So per job, in
   order:
   (i) **pause the Hermes twin and drain** any in-flight run — the day's
       production occurrence is now owned by exactly one scheduler;
   (ii) **acceptance gate (blocking):** on the real Windows task —
       (a) a nonzero launcher exit propagates to `LastTaskResult`
       through the vbs shim (deliberately break the script once, writing
       to an **isolated** output, not the production target);
       (b) job-watch pages on that failure and on a simulated overdue;
       (c) a failed page retries; (d) `ExecutionTimeLimit` terminates a
       hung run; (e) a missed-while-off run replays or skips per policy;
       (f) one **normal** run produces the expected side effect exactly
       once (safe now — Hermes is paused, so this is the sole owner);
   (iii) leave the Windows task enabled; watch one full day of real
       triggers.
   Injected-failure tests use isolated outputs; the one real production
   occurrence is always assigned to a single scheduler.
6. **Retire Hermes** only after all three are stable for a full cycle,
   BOTH scheduler-observer drills (step 1) passed, AND a full
   gateway-consumer inventory (not just the cron audit — enumerate any
   other consumers, integrations, and autostart paths of the Hermes
   gateway) confirms nothing else depends on it: set `agent-gateway`
   `enabled=false`, stop the gateway, and (after Suti's ok) remove the
   Hermes autostart. Verify the rollback restore from the step-0
   snapshot in a dry run BEFORE deleting the live Hermes store.
7. **Heartbeat:** the paused `narada-heartbeat` definition is already in
   the step-0 snapshot; that is its archive. Let the live store go with
   the rest.

## Rollback (single-owner in both branches — round-2 finding 3)

Every rollback path must leave exactly one scheduler owning each
occurrence — the same discipline as cutover.

Before retirement (step 6): for the affected job(s), **disable the
Windows trigger, drain any in-flight run, THEN re-enable the Hermes job**
(pause-first, reversed). Never re-enable Hermes while the Windows task
can still fire the same occurrence.

After retirement: **first disable/unregister the three Windows tasks and
drain in-flight work, THEN restore the step-0 snapshot with the Hermes
jobs re-created INITIALLY PAUSED**, and resume only occurrences not
already served that day (avoid a same-day double debrief/check-in).
Snapshot availability alone is not sufficient — this ordered sequence
is itself part of the restore acceptance test (step 0's snapshot is
validated by dry-running exactly this restore before any cleanup).

## Deferred (named, out of scope for this cutover)

- **Figures detached-completion monitoring** — the tick's exit proves it
  launched, not that the 20–40-min generation succeeded (a Hermes-era
  blind spot, unchanged here). Future: the generation writes a
  completion record; a watcher pages on a missing/failed completion.
- **Automatic crash-retry for delivery jobs** — requires durable
  per-occurrence dedup (atomic cross-process claim) before it is safe.
- **External dead-man's-switch** — the only cover for box-fully-down /
  wedged-before-logon (the box pings an off-box service every N min;
  that service pages on silence). Independent of the Hermes question;
  its own plan.

## Round-1 cross-review (Codex gpt-6-astra, 2026-09-07)

Verdict needs-attention; 5 high + 1 medium; ALL ACCEPTED:
1. Enable-first double-fire on delivery jobs → **pause-first + drain**,
   no auto-retry for delivery jobs, dedup deferred. (§constraints,
   §target, step 5)
2. Observer shares the scheduler's failure domain → **host-side
   scheduler observer** reading a deadman progress file, + a
   Scheduler-stopped drill. (§Scheduler observer, step 1)
3. Overdue-only hides figures launch failures → **result+overdue for
   all three**; detached-completion kept separate. (§target, §Deferred)
4. Cleanup removes rollback artifacts → **step-0 full snapshot** +
   verified restore test gating cleanup. (step 0, step 6, §Rollback)
5. Dropped the addendum's failure-path gates → **blocking per-task
   acceptance** (nonzero propagation, timeout, missed-run, overdue
   detection, failed-page retry) before retiring Hermes. (step 5)
6. Log preservation unspecified → **durable per-job logs** + outcome
   records; figures child output destination verified. (§Logging)

Next-steps also folded in: explicit multiple triggers (6/3/1);
timezone/DST recorded (host TZ has no DST); gateway-consumer inventory
before shutdown (step 6 precondition — the audit is cron-only).

## Round-2 cross-review (Codex gpt-6-astra, 2026-09-07)

Verdict needs-attention; 3 high (all deeper applications of round-1's
single-owner rule); ALL ACCEPTED. Round 1's logging, archive gates,
retry removal, and figures result+overdue were confirmed adequate.
1. Acceptance testing reintroduced the double-fire window (a
   production-side-effect test ran before Hermes was paused) →
   **pause-first now applies to the acceptance phase**: per job, pause +
   drain the Hermes twin BEFORE any acceptance test; injected-failure
   tests use isolated outputs; the one production occurrence is owned by
   a single scheduler. (step 5, rewritten)
2. Observer shared a **bootstrap** failure domain (Scheduler dead at
   boot launches neither host nor deadman) → **dual-bootstrap the host**
   (Task Scheduler + Startup-folder shortcut, idempotent via the host
   lockfile) so a Scheduler-dead-at-boot still starts the observer; add
   a second drill (Scheduler unavailable *before* host startup); the
   box-fully-down case is named and deferred to an external
   dead-man's-switch. (§Scheduler observer, §Deferred)
3. Rollback didn't preserve single-owner (could restore two active
   schedulers) → **both rollback branches disable Windows triggers +
   drain first, restore Hermes INITIALLY PAUSED, resume only unserved
   occurrences**, and this sequence is itself the restore acceptance
   test. (§Rollback)

**Plan debate closed here per the cross-review protocol** (round 1 +
one further check). All findings across both rounds were accepted and
folded in; none rejected. The plan ends at needs-attention with every
finding addressed in the spec — the addressed items become the
acceptance criteria verified at implementation-diff-review time. Ready
for Suti's go / no-go on execution.
