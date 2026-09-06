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

## Step 3 — smriti logd handoff: NOT STARTED (blocked on step 2 drills)
## Step 4 — Hermes absorbs morning/nightly: NOT STARTED (blocked on 3;
   prerequisite: diagnose why both jobs currently exit 1)
## Step 5 — cognition convergence: tracked in its own plans
