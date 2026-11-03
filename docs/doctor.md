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
| `brain_sessions` | each Claude brain session's resume target exists in `~/.claude/projects` | (amber) pruned transcripts: next use returns 500 |
| `telegram_bridge` | `getUpdates` recency in host.log | no successful poll in 15 min; a 409 conflict |
| `telegram_inbound` | last exchange in `heartbeat/chat-cycles`, and whether the Telegram brain session can resume | last exchange FAILED, or its resume target was pruned |
| `livekit` | `:7880` locally, `wss :7443` over the tailnet | local down (tailnet down is amber) |
| `box_dispatch` | LiveKit participants in `narada-body` plus the worker's job outcomes in host.log | BOX present with no agent; the wake-model bail loop |
| `voice_box` | worker `:8792`, wake model file, `voice-fault.json`, budget ledger | worker down, model missing, credits fault |
| `voice_phone` | worker `:8796`, shared voice fault | worker down, credits fault |
| `box_device` | BOX participant present, drops in 24 h (alert DB) | absent for more than 3 min |
| `push_subs` | devices in `brain/push-subs.json` | zero |
| `smriti_nightly` | `log/.nightly-status.json`; failure streak from deadman job-watch | didn't run last night, or any step failed (named) |
| `smriti_tasks` | logd pid alive and state fresh; today's morning sent | logd dead; morning not sent |
| `qmd_index` | newest document in qmd's index (read-only sqlite) | newest document older than 7 days |
| `akhada_tick` | Narada_Akhada_Checkin cron log | last rc≠0; no run for 14 h |
| `memory_backup` | `~/.narada` commits ahead of `@{u}` and their age | oldest unpushed older than 48 h, or ≥50 unpushed |
| `deadman` | deadman.log recency, restart budget, can it page | silent 15 min; budget exhausted |
| `host_alerts` | A3 alert DB: open episodes, stuck outbox | undelivered alerts older than 15 min |
| `scheduled_tasks` | other Narada_* tasks' last result; debrief false green | (amber only) |
| `log_hygiene` | bot token in host.log | (amber only) |

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

Rendering guidance (the app side is not built):

- Title: `headline`, with a dot in the `overall` colour.
- Red and amber items are expanded, showing reason, since and action. Green items collapse into "N fine".
- **Stale state:** if `now - generated_at > stale_after_s`, grey the card and show "doctor last ran {age} ago". This is the honest offline state W4 asks for: an old card never looks current.
- Times show in the phone's local zone. When `since_approx` is set, prefix "about".

**Serving it (not built).** A brain route `GET /api/doctor/card` would return `doctor.json["card"]`. It goes behind the same bearer tiers and, after W4, the same Access policy, and it must be added to the W4 route inventory. Until an hourly `--write` job exists, the card is only as fresh as the last manual run.

## Daily digest (designed, not enabled)

**Goal:** one message a day, only when something is red. It names what failed, including the failing step for the nightly, when it started, and the next action.

Already built and tested (`prana/doctor/digest.py`); there is no sending code:

- `compose_digest(report, tz)` returns the text, or None when nothing is red. It stays under 3,500 characters and is redacted.
- `digest_due(report, state, today)` is true only when something is red, at most once per local day.
- `fingerprint(report)` is stable while the same checks are red for the same reasons, so a repeat on a later day can say "day 3".

`--digest` prints exactly what would be sent today. Example:

```
Narada doctor, Tue 03 Nov: 7 red.
- smriti nightly: last night (2026-11-02) failed at step index: No embedding provider available (...) (since ~26 Oct 03:06)
  next: smriti audit 2026-11-03: the index step needs numpy<2 (...)
- Telegram inbound → reply: last exchange 2026-10-23 12:17 FAILED (brain 500 ...) (since 23 Oct 02:02)
  next: in Telegram send /new (fresh brain session); ...
```

To enable it later, deliberately:

1. Add a `--send-digest` path to `__main__.py`. It runs the checks and loads `~/.narada/host/doctor-digest.json`. If `digest_due(...)`, it pipes the text to smriti's single notify point with `NOTIFY_LEVEL=error` and runs `python ~/.narada/.smriti/hooks/notify_suti.py` (stdin). That is the script smriti's `tell_suti` MCP tool wraps, so the message fans out to Telegram and the app feed/push. Write `{"day", "fingerprint", "sent_at"}` only when notify_suti exits 0, and exit nonzero otherwise so the task result tells the truth.
2. Register a `Narada_Doctor_Digest` task at **08:30 Brisbane**, after smriti's 07:00 morning message and before the 11:00 akhada tick. Use the `install-cron-tasks.ps1` pattern: `wscript run-hidden.vbs pythonw cron_run.py <log> scripts/doctor.py --write --send-digest`.
3. Test with a forced red (for example `--only push_subs` while push-subs.json is empty), then confirm a second run the same day sends nothing.

**Why not `tell_suti` directly:** it is an MCP tool and needs a live Claude session. A scheduled job calls the hook it wraps, and delivery is identical.

**Limit:** if the red thing is Telegram outbound itself, or the PC is off, this digest can't arrive. That is what the external switch below is for.

## External dead-man's switch (designed, not enabled)

**Goal:** silence itself raises the alarm, even when the PC is off, asleep, has lost its network, or Task Scheduler has died. The alarm comes from a service that isn't on this PC, through a Telegram bot that isn't Narada's.

**Service:** [Healthchecks.io](https://healthchecks.io), Hobbyist plan (free; 20 checks, which is plenty). Each job "pings" a URL when it runs. If a ping doesn't arrive on schedule plus a grace period, Healthchecks alerts through its own integrations: email by default, plus Telegram through Healthchecks' own bot.

### Which jobs ping, and when

| Healthchecks check (slug) | Pinged by | When | Schedule in Healthchecks | Grace |
|---|---|---|---|---|
| `narada-pc` | `deadman.ps1`, every tick, unconditionally | every 5 min | Simple, period 5 min | 15 min |
| `narada-host` | `deadman.ps1`: success when the host beacon is fresh, `/fail` when stale | every 5 min | Simple, period 5 min | 15 min |
| `smriti-nightly` | the nightly task via `cron_run.py`: `/start`, then `/<exit code>` | 03:00 | Cron `0 3 * * *`, tz Australia/Brisbane | 60 min |
| `smriti-morning` | the morning task via `cron_run.py`: `/<exit code>` | 07:00 | Cron `0 7 * * *`, Brisbane | 45 min |
| `akhada-checkin` | `cron_run.py` for Narada_Akhada_Checkin: `/<exit code>` | 11:00, 18:00, 22:00 | Cron `0 11,18,22 * * *`, Brisbane | 30 min |
| `narada-doctor` | an hourly `scripts/doctor.py --write`: success if nothing is red, `/fail` if red (body = the digest text) | every hour at :15 | Cron `15 * * * *`, Brisbane | 30 min |
| `memory-backup` | `~/.narada/.smriti/backup.py`, after a successful `git push` only | on every push (several a day) | Simple, period 1 day | 1 day |

What each catches:

- **`narada-pc`**: PC off or asleep, network down, Task Scheduler dead, the deadman itself broken.
- **`narada-host`**: the supervisor dead or hung, even when the deadman can't page (its Telegram path is Narada's own).
- **The smriti and akhada checks**: a job that failed or never ran.
- **`narada-doctor`**: every *functional* red from the doctor (Telegram inbound, the BOX bail loop, push, qmd), delivered off-box.
- **`memory-backup`**: pushes silently failing. `backup.py` exits 0 on a push error, which is how 78 commits piled up unnoticed from 10-26.

Ping URL shape: `https://hc-ping.com/<ping-key>/<slug>`, plus `/start`, `/fail` or `/<0-255>`. One project-level ping key serves every check.

### Setup steps for Suti (about 20 minutes)

1. **Account.** Sign up at healthchecks.io on the free Hobbyist plan. One project, for example "Narada home".
2. **Ping key.** Project Settings, then API Access: create a **Ping key**. Save it on the PC as a one-line file at `%LOCALAPPDATA%\narada\healthchecks.key`. It stays out of `~/.narada`, so it never reaches git. Treat it as a secret: it can mark checks up or down.
3. **Alerts.** Integrations: keep Email, and add **Telegram**. Follow the prompts; it connects through Healthchecks' own bot, independent of Narada's bot token. Optionally add a second channel, such as the Pushover or ntfy app on the phone.
4. **Create the seven checks** from the table: Add Check, set the **slug** exactly as listed, choose Simple or Cron, set the **time zone to Australia/Brisbane** for cron checks, and set the grace period. Attach the integrations.
5. **Tell Narada "go"**, and the wiring lands as one small change on a branch, reviewed like any other:
   - `deadman.ps1`: a `Send-HcPing` function. It does nothing when the key file is absent, uses a 10 s timeout, and logs failures to deadman.log without throwing. It pings `narada-pc` every tick, and `narada-host` or `narada-host/fail` after the beacon check:
     ```powershell
     function Send-HcPing([string]$Slug, [string]$Suffix = '') {
         $kp = "$env:LOCALAPPDATA\narada\healthchecks.key"
         if (-not (Test-Path $kp)) { return }
         try {
             $key = (Get-Content $kp -TotalCount 1).Trim()
             Invoke-WebRequest -Uri "https://hc-ping.com/$key/$Slug$Suffix" -Method Post `
                 -TimeoutSec 10 -UseBasicParsing | Out-Null
         } catch { Write-DeadmanLog "hc ping $Slug failed: $($_.Exception.GetType().Name)" }
     }
     ```
   - `cron_run.py`: after the child exits, if the key file exists and the job's log stem is mapped (`Narada_Akhada_Checkin` to `akhada-checkin`, and so on), POST to `/<slug>/<rc>` with the last 10 kB of output as the body, using a 10 s timeout. Errors are swallowed.
   - Re-point the `smriti-nightly` and `smriti-morning` task actions through `cron_run.py`: `python cron_run.py <log> -m smriti nightly`. This also gives the nightly a captured log, which it lacks today.
   - `backup.py`: ping `memory-backup` only when `git push` returned 0.
   - Register `Narada_Doctor` hourly. The doctor pings `narada-doctor` itself, red → `/fail`.
6. **Rehearse.** Run each job once by hand (Task Scheduler, Run) and watch the checks turn green in Healthchecks. Then prove the alarm path:
   - set `narada-pc`'s period to 1 min and grace to 1 min, and disable Narada_Deadman for 5 minutes. A Telegram alert from Healthchecks' bot must arrive. Re-enable it and restore the period.
   - unplug the network, or shut the PC down for 20 minutes. `narada-pc` and `narada-host` must alert.

Nothing above has been done: no account, no key, no task or script changes.
