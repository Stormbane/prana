# narada doctor

Read-only stability checks over Narada's home system. Built 2026-11-03 after Suti said "for this to really work, we need it rock solid stable. There's quite a few little pieces... things that fall apart every now and then."

The supervisor already watches **liveness**: is the process up, does the port answer. On 2026-11-03 it reported every component healthy, yet the BOX worker bailed on every job and the brain returned 500 on every Telegram turn. The doctor checks **function** instead: an agent actually in the BOX's room, a brain session that can resume, a nightly that finished every step.

## Running it

```
python scripts/doctor.py              # human summary; works from any checkout
python -m prana.doctor                # same, via the installed package
  --write      also write ~/.narada/host/doctor.json (atomic temp+replace)
  --json       print the full report JSON
  --card       print the phone status card JSON
  --digest     print the red digest that WOULD be sent (never sends)
  --only a,b   run only these check ids
  --timeout S  deadline for the whole run (default 20 s)
```

Exit code: 0 green, 1 amber, 2 red. A typical run takes about 1.5 s.

**Safety.** All I/O goes through `prana.doctor.probes.Probes`, which:

- never restarts, reconfigures or messages anything;
- opens sqlite with `mode=ro`;
- runs git with `GIT_OPTIONAL_LOCKS=0` and only `rev-parse`, `rev-list` and `log`, never `fetch` or `push`;
- bounds every HTTP call and subprocess with a timeout;
- signs a short-lived LiveKit token in-process for two read-only RoomService calls (`ListRooms`, `ListParticipants`), never returning, logging or raising the secret;
- passes every output string through `model.redact()`.

Each check runs in its own daemon thread under the run deadline. A check that crashes or overruns is reported AMBER ("unknown"), never red and never green.

## The checks

| id | What it judges | Red when |
|---|---|---|
| `host` | Narada_Host heartbeat beacon, pid | beacon >5 min stale, pid dead, no beacon |
| `components` | every enabled component's `health_url` | any enabled component doesn't answer |
| `brain_local` | `127.0.0.1:8811/health` | not `{"ok": true}` |
| `brain_tailnet` | `https://<tailnet>:8443/health` | unreachable |
| `brain_sessions` | each Claude brain session's resume target exists in `~/.claude/projects` | (green only) pruned transcripts are noted: since prana `51a3f27` the brain starts those fresh with a handover |
| `telegram_bridge` | `getUpdates` recency in host.log | no successful poll in 15 min; a 409 conflict newer than the last good poll. No poll lines at all (request logging off) is amber |
| `telegram_inbound` | last exchange in `heartbeat/chat-cycles` | last exchange FAILED (amber when the brain has restarted since and nothing has tested it) |
| `livekit` | `:7880` locally, `wss :7443` over the tailnet | local down (tailnet down is amber) |
| `box_dispatch` | LiveKit participants in `narada-body` plus the worker's job outcomes in host.log | BOX present with no agent; the wake-model bail loop |
| `voice_box` | worker `:8792`, wake model file, `voice-fault.json`, budget ledger | worker down, model missing, credits fault (only a good BOX session clears it; a later budget entry is no proof) |
| `voice_phone` | worker `:8796`, shared voice fault | worker down, credits fault |
| `box_device` | BOX participant present, drops in 24 h (alert DB) | absent for more than 3 min |
| `push_subs` | devices in `brain/push-subs.json` | zero |
| `smriti_nightly` | `log/.nightly-status.json`; failure streak from deadman job-watch | didn't run last night, or any step failed (named) |
| `smriti_tasks` | logd pid alive and state fresh | logd dead |
| `morning` | the morning message, **the daily heartbeat Suti notices** (`log/.morning-status.json`) | not sent for the due day (07:30 local); names the failed phase: composing or delivery |
| `akhada_tick` | Narada_Akhada_Checkin cron log | last rc≠0; no run for 14 h |
| `memory_backup` | `~/.narada` commits ahead of `@{u}` and their age | oldest unpushed older than 48 h, or ≥50 unpushed |
| `deadman` | deadman.log recency (job-watch lines ignored), restart budget, can it page | silent 15 min; budget exhausted |
| `host_alerts` | A3 alert DB: open episodes, stuck outbox | undelivered alerts older than 15 min |
| `scheduled_tasks` | other Narada_* tasks' last result; debrief false green | (amber only) |
| `log_hygiene` | bot token in host.log | (amber only) |

*Retired 2026-11-04: `qmd_index`. smriti dropped its qmd refresh step (smriti `cafb9d5`): qmd's only consumer, the ambient-recall hook, was switched off after an eval found 18 of 20 injections unrelated, so a stale qmd index is no longer a fault.*

"since" is when the current state began, as best the evidence shows. `since_approx: true` marks a lower bound or an inference, for example log retention or the mtime of the directory a missing file was in.

## doctor.json (`--write`)

Written to `~/.narada/host/doctor.json`:

```json
{
  "schema": "narada.doctor/1",
  "generated_at": "2026-11-03T13:43:42Z",
  "host": "<hostname>",
  "overall": "red",
  "counts": {"green": 11, "amber": 4, "red": 7},
  "checks": [
    {
      "id": "smriti_nightly",
      "title": "smriti nightly",
      "status": "red",
      "reason": "last night (2026-11-02) failed at step index: No embedding provider available (...)",
      "since": "2026-10-25T17:06:00Z",
      "since_approx": true,
      "hint": "smriti audit 2026-11-03: the index step needs numpy<2 (...)",
      "details": {"failed_steps": ["index"], "target_day": "2026-11-02"},
      "duration_ms": 41
    }
  ],
  "card": { "...": "see below" }
}
```

## Status card (phone app, Data tab)

The doctor puts this object under `card` in doctor.json, and `--card` prints it. It is self-contained: the app renders it without knowing any check.

```json
{
  "kind": "narada.status-card",
  "version": 1,
  "generated_at": "2026-11-03T13:43:42Z",
  "stale_after_s": 10800,
  "overall": "red",
  "headline": "7 need attention: Telegram inbound → reply, BOX voice: agent dispatch, BOX voice worker…",
  "counts": {"green": 11, "amber": 4, "red": 7},
  "items": [
    {
      "id": "telegram_inbound",
      "label": "Telegram inbound → reply",
      "status": "red",
      "reason": "last exchange 2026-10-23 12:17 FAILED (brain 500: Internal Server Error); ...",
      "since": "2026-10-22T16:02:32Z",
      "since_approx": false,
      "action": "in Telegram send /new (fresh brain session); ..."
    }
  ]
}
```

| Field | Type | Meaning |
|---|---|---|
| `kind`, `version` | string, int | the app checks both; it ignores an unknown version |
| `generated_at` | ISO 8601 UTC | when the doctor ran |
| `stale_after_s` | int | after this long, the card is stale |
| `overall` | `green` \| `amber` \| `red` | the worst item |
| `headline` | string | one line for the card's title |
| `counts` | object | number of items per status |
| `items[]` | array | ordered worst first: red, amber, green |
| `items[].label`, `.reason` | string | already human-readable and redacted |
| `items[].since` | ISO 8601 UTC or null | when this state began; `since_approx` adds "about" |
| `items[].action` | string or null | the next thing to do, when known |

Rendering guidance:

- Title: `headline`, with a dot in the `overall` colour.
- Red and amber items are expanded, showing reason, since and action. Green items collapse into "N fine".
- **Stale state:** if `now - generated_at > stale_after_s`, grey the card and show "doctor last ran {age} ago". This is the honest offline state W4 asks for: an old card never looks current.
- Times show in the phone's local zone. When `since_approx` is set, prefix "about".

**Serving it: `GET /api/doctor`** (`prana/brain/doctor_api.py`). This is the one door: the app asks it, and the CLI, the digest and the wake briefing read the same `run_checks`.

- It returns the card. If the last run is older than 60 s it reruns the checks first, in a worker thread, so the brain still answers its own `/health` probe. `?fresh=1` (pull to refresh) reruns once the card is 10 s old.
- One run at a time; concurrent callers share it. A failed run serves the last card, still honestly dated by `generated_at`; a failed first run is a 502.
- Every run also writes `~/.narada/host/doctor.json`, which the wake briefing reads.
- Tiers: `app` and `prana`, not `voice`. After W4 it goes behind the same Access policy, and it must be added to the W4 route inventory.

## Daily digest

**Goal:** one message a day, only when something is red. It names what failed, including the failing step for the nightly, when it started, and the next action.

`python scripts/doctor.py --write --send-digest` (`prana/doctor/send.py`):

- runs every check, writes `doctor.json`, and sends only if `digest_due`: something is red, and no digest has gone out today (`~/.narada/host/doctor-digest.json`);
- sends through **smriti's single notify point**, `~/.narada/.smriti/hooks/notify_suti.py` (the script the `tell_suti` MCP tool wraps), with `NOTIFY_LEVEL=error` and `NOTIFY_SOURCE=doctor`, so it reaches Telegram and the app and lands in the System log like any other fault;
- writes the state only when notify_suti exits 0. A failed delivery exits 3, so the scheduled task's result tells the truth, and the next run retries;
- marks a repeat: when the same checks are red for the same reasons as yesterday, the digest starts "(Unchanged, day N.)".

`--send-digest` is the only flag that sends anything. `--digest` prints what would be sent and never sends.

**Schedule:** `Narada_Doctor_Digest` at **08:30 Brisbane**, after smriti's 07:00 morning message and before the 11:00 akhada tick, installed by `scripts/install/install-doctor-digest.ps1` (the `install-cron-tasks.ps1` pattern: hidden, through `cron_run.py`, so the run has a captured log).

## Why there is no external dead-man's switch

Suti, 2026-11-04: no extra accounts or services (Healthchecks.io was proposed and rejected). Instead:

- **The morning message is the heartbeat.** It arrives every day at 07:00. If it doesn't, Suti notices, and that covers the cases nothing on this PC can report: the PC off, the network down, Telegram outbound broken. The `morning` check names why it failed when the PC is up to say so.
- **The digest names what is red**, at most once a day.
- **The local deadman** (`deadman.ps1`, every 5 minutes) restarts a dead host and pages through Telegram when its restart budget runs out.
