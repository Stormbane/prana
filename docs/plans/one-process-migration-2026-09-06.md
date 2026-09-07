# One-process migration — execution log

**Governing spec:** `personal-agent-platform-2026-09-04.md`, Addendum
(cross-reviewed 2026-09-06, rounds 1+2). Steps here follow the
addendum's migration order. This is a log, not a plan — it records what
was found and done, so canon and machine agree.

## Step 1 — autostart audit (2026-09-06, complete)

**Windows Scheduler** (the only autostart mechanism still in use):

| Task | State | Verdict |
|---|---|---|
| `Narada_Host` | Running (logon trigger, restart x3/1min, no time limit) | Stays — bootstrap exception. Restart-on-failure covers crashes only; hang coverage arrives with the deadman (step 2). |
| `smriti-logd` | Every 10 min, `-m smriti logd --ensure`, LastResult 0 | Absorb via single-owner handoff (step 3). The `--ensure` task is the current keep-alive for the logd daemon (live pid found). |
| `smriti-morning` | Daily 07:00, **LastResult 1 — FAILING** | Absorb into Hermes (step 4). ⚠ Currently failing silently — the exact zombie shape the addendum exists to kill. Root cause lives in the smriti repo; must be diagnosed before or during absorption (a migrated-but-failing job is not a migration). |
| `smriti-nightly` | Daily 03:00, **LastResult 1 — FAILING** | Same. Matches the wake briefing's "NIGHTLY HAD FAILURES" notice — the notice fires, but nothing pages. |

**Startup folders / Run keys:** clean of Narada entries (Stage 5
migration held). Ollama, Tailscale, Docker etc. are platform-level and
out of scope.

**Hermes cron store** (`~/.hermes/cron/jobs.json`): `narada-heartbeat`
(paused 2026-08-06, stays paused — heartbeat resurrection is out of
scope per CLAUDE.md), `bt-figures-populate` (Beautiful Tree concern,
out of scope). No orphaned Narada cron entries to absorb.

**Live process reconciliation:** every registry component accounted for
under the host (agent-gateway, chat-bridge, sessions, livekit, voice,
voice-phone, akhada-dashboard, brain, akhada-brain; deha-brain
disabled). Additional live processes are all **per-session MCP children**
(smriti.mcp_server ×6, akhada.adapters.mcp_server ×3, deha speak_mcp ×5
— spawned and owned by Claude/Codex sessions, NOT daemons to
supervise) plus the task-kept `smriti logd` daemon.

**Registry-delta clarification (corrects the addendum's step-1 guess):**
the "smriti MCP/index daemon" named in the audit deltas is not a
supervisable daemon — the MCP server is spawned per client session, and
its 09-05/06 outage was a code/env fault in that spawn path, not a dead
process nobody restarted. The supervisable smriti pieces are **logd**
(daemon, step 3) and **morning/nightly** (scheduled jobs, step 4). The
per-session MCP spawn failure mode is real but belongs to the smriti
repo (make the spawn fail loud), not to the registry.

## Step 2 — deadman (2026-09-06, implemented; ARMING GATED)

Commit `6fc625f`:

- Supervisor writes `%LOCALAPPDATA%\narada\host.heartbeat` every 30 s
  from its own event loop (atomic tmp+replace; a wedged loop stops the
  beacon by design).
- `scripts/install/deadman.ps1` — pure PowerShell 5.1, zero stack
  dependencies. Stale (>300 s) or missing beacon → restart
  `Narada_Host` (stop-first when hung; the host's kill-on-close job
  object takes the component tree down with it). Budget: 3 restarts per
  rolling hour, then stand down + page once; fresh beacon clears the
  ledger and pages recovery. Pages ride the bridge's Telegram token
  (`~/.hermes/.env`, line-parsed — no dotenv).
- `Narada_Deadman` task registered **DISABLED** (`prana host
  install-deadman`): the live host predates the beacon, so arming now
  would bounce a healthy stack.
- Tests: 8 new (heartbeat write/refresh/stop; deadman decisions driven
  through real powershell in dry-run: fresh/stale/missing/exhausted/
  already-paged/window-pruning/recovery). Full suite 251 passed.

**Implementation cross-review (Codex gpt-6-astra, 2026-09-06):**
diff review of `6fc625f` → needs-attention, 3 findings, all accepted
and fixed in `29bb691` (persist-the-ledger-BEFORE-restart with refusal
on unwritable/corrupt state; exhaustion as a true latch cleared only by
verified recovery; latch set only on confirmed page delivery) + a mock
harness so the mutating paths run in tests. Recheck found one residual
(empty/truncated ledger parses to $null without exception, bypassing
the corrupt-state refusal) → fixed in `69675bb`. Final recheck:
**approve**. Task XML verified: PT5M interval, no Duration —
repetition indefinite. 17 deadman tests; suite 260 passed.

**Step-2 gate — CLOSED (2026-09-06, all drills passed live):**
1. ✅ `Narada_Host` bounced; `host.heartbeat` appeared within 30 s and
   refreshes every 30 s. Brain + LiveKit back at 200 automatically.
2. ✅ `Narada_Deadman` armed; first scheduled run logged
   `healthy (age 19s)`, exit 0.
3. ✅ **Hung-host drill:** host process suspended (event loop frozen,
   process alive — the true hang signal); beacon went stale; the
   deadman detected `beacon stale 44s` on a Running task, stopped the
   frozen host (job object took the component tree with it), started a
   fresh one (pid 7260 → 16264 → healthy), Telegram page delivered,
   ledger recorded exactly 1 restart. Old suspended pid confirmed
   killed.
4. ✅ **Exhaustion drill:** ledger seeded with 3 recent restarts, host
   suspended to stale the beacon. Run 1: stood down (host pid
   unchanged — refused to restart), paged once, latched. Run 2: silent
   (`exhaustion latched`), no page, no restart — the latch holds
   despite the drill using `-StaleSeconds 30`.
5. ✅ **Recovery:** host resumed → fresh beacon → deadman cleared the
   ledger and sent the all-clear page.

The manual drill invocations used `-StaleSeconds 30` (vs the task's
300 s default) to keep the drill tight; the stale-detection code path
is identical. Final live state: deadman Ready/armed, ledger clean, all
five probed components 200. **Consolidation (steps 3–4) is now
unblocked** per the addendum's gate.

## Step 4a — external job-freshness watcher (2026-09-07, done)

The addendum requires overdue-job detection checked OUTSIDE the
scheduler. Built it BEFORE the Hermes cutover so the silent-failure gap
closes immediately, against the current Windows tasks — the exact gap
that just bit us (the wake briefing said "NIGHTLY HAD FAILURES" and
nothing paged Suti).

- `scripts/install/job-watch.ps1` — pure PS 5.1, read-only + page-only
  (never restarts a job). Reads a config of watched jobs; for each,
  queries the Windows task and pages once per distinct problem
  (failed / overdue / missing), latches per-job on confirmed delivery,
  announces recovery and clears the latch. Registered as the SECOND
  action on `Narada_Deadman` (one external entry, two dumb scripts run
  in sequence each tick) — no edits to the reviewed host-liveness core.
- Config `job-watch.template.json` → seeded to
  `%LOCALAPPDATA%\narada\job-watch.json` by the installer (never
  overwrites an edited config).
- Tests: 11 (mock harness shadows `Get-ScheduledTaskInfo` +
  `Invoke-RestMethod`; healthy/failed/overdue/missing/running/
  still-broken/recovery/page-failure/multi-job/overdue-only). Suite
  298 passed.
- Live drill: dry-run correctly flagged nightly's stale pre-fix failure
  and stayed silent on morning/logd; after triggering nightly, the full
  `Narada_Deadman` task ran both actions in sequence, result 0, nothing
  falsely latched.
- Cross-review (Codex gpt-6-astra): diff review found 3 (config-failure
  silently disables monitoring; in-flight running/queued misclassified;
  lost recovery page) — all fixed. Recheck found 1 residual (empty/null
  config parses to `$null` in PS 5.1 — the SAME trap as the deadman
  ledger) — fixed. Final recheck: **approve**. Commits `b62ba03` →
  `195574e` → `41ec317`.
- Launcher: both `Narada_Deadman` actions run through `run-hidden.vbs`
  (a parallel session's fix, verified + adopted here) so the 5-minute
  tick never flashes a console. Exit-code propagation checked (7 and 0).

**Finding for the smriti side (cross-repo):** `smriti morning` exits
**1 when the message was already sent today** — a success invariant
("morning was sent today" holds), not a failure. Exit code alone is
therefore an ambiguous health signal for it (1 = already-sent vs 1 =
real error are indistinguishable). Worked around here by watching
morning `overdue-only` (did it fire within 26h — the true health
signal for a once-daily idempotent job). The clean fix belongs in
smriti: reserve nonzero for real errors and exit 0 (or a distinct
"already done" code) on the benign case. `smriti nightly` has clean
exit semantics (verified exit 0 with all stages ok, 2026-09-07) and is
watched `result+overdue`.

## Step 3 — smriti logd handoff: NOT STARTED — needs a smriti-side decision

The addendum assumed logd was a simple daemon to supervise. It is not:
`smriti logd --ensure` (the `smriti-logd` task, every 10 min) does TWO
things — (a) keepalive for the logd daemon, and (b) **keepalive for the
qmd recall daemon**, added in smriti commit `60449be` (2026-09-06).
Naively supervising `smriti logd` under the prana host and disabling
the task would silently drop the qmd keepalive — undoing a fresh design
decision from the smriti session. **This needs coordination with the
smriti owner**, not a unilateral handoff. Options to weigh with Suti:
(i) supervise the logd daemon under the host AND give qmd its own
supervised component; (ii) leave logd on its self-recovering `--ensure`
tick (it already restarts itself) and only *watch* it via step 4a —
which is now done. Option (ii) may make step 3 unnecessary.

## Step 4 — Hermes absorbs morning/nightly: NOT STARTED — needs a decision

Prerequisites now met (both jobs verified working; external overdue
detection built). But the cutover moves live memory-hygiene onto
Hermes, whose own heartbeat job has been **paused since 2026-08-06** —
the addendum's finding 2 ("a new single point of failure worse than
Windows Scheduler's independence") is live here. **Open question for
Suti:** is the Hermes cutover worth it, or is the better end-state
"keep morning/nightly on Windows Scheduler (independent, proven) but
now watched from outside by step 4a"? The watcher delivers step 4's
safety value without taking the cutover risk. Recommend NOT cutting
over unless there is a concrete reason to consolidate the scheduler.

## Retire Hermes (retire-hermes-2026-09-07 plan) — execution status

**Plan cross-reviewed** (2 rounds, all accepted). **All code written,
committed, and diff-reviewed** (observer: 4+2+1 findings across 3
rechecks → approve; steps 2-4: 3 findings → approve). Commits from
`f63a547` through `58f7f21`.

**Done and live (safe, reversible, nothing cut over):**
- Rollback snapshot: `docs/archive/hermes-2026-09-07/`.
- Scheduler observer CODE committed: deadman writes `deadman.progress`;
  host reads it + pages via the alert outbox; dual-bootstrap
  (Startup-folder shortcut) installed. **NOT yet live** — see below.
- Figures tick moved into `scripts/populate_figures_tick.py`, OUT_DIR →
  `~/.narada/figures/`.
- Three Windows tasks registered **DISABLED** (born disabled, atomic):
  `Narada_Figures` (6 triggers), `Narada_Akhada_Checkin` (3),
  `Narada_Daily_Debrief` (1). Inert — they do not fire.
- Live `job-watch.json` updated to watch all six; the three cron tasks
  carry `disabled_ok:true` and are correctly skipped (verified live, no
  false pages).

**REMAINING — all disruptive to the live box; paused for Suti's timing
(2026-09-07: box in active use, live voice on narada-body observed):**
1. **Bounce Narada_Host onto the observer build.** The running host
   (pid 15288, started 2026-09-06 23:24) predates the observer code; a
   Stop/Start could not replace it cleanly and a hard kill would
   interrupt live voice. Needs a quiet moment.
2. **Per-job cutover** (pause-first): for each job, pause the Hermes
   twin + drain, run the acceptance gate on the Windows task, enable it,
   drop its `disabled_ok`. Low-disruption but should follow (1).
3. **Two blocking observer drills:** (a) stop the Task Scheduler service
   with the host up → host pages deadman-staleness (system-wide, ~min);
   (b) Scheduler-dead-at-boot → Startup shortcut brings the host up and
   it pages (needs a reboot/logoff — disruptive).
4. **Retire Hermes** (step 6): `agent-gateway` enabled=false, stop the
   gateway, verified rollback dry-run, remove autostart.

The intermediate state is clean: Hermes still runs all three jobs; the
Windows tasks are inert; the observer is coded but dormant. Fully
reversible by unregistering the three disabled tasks.

## Step 5 (one-process) — cognition convergence: tracked in its own plans
