# Doctor, BOX and Telegram inbound: audit, 2026-11-03

Branch `doctor` (worktree `C:\projects\prana-wt-doctor`, from `briefing` @ `cb419dc`). Read-only diagnosis of two live failures, plus a new `narada doctor`. Times are Brisbane (UTC+10) unless marked Z.

## Summary

- **The BOX: root cause found.** On **2026-10-22 at about 20:59**, a disk-cleanup session (`C:\Projects\gpu_tests`) moved `~/.narada/wakeword/output` to `D:\Archive\wakeword\output`. The BOX worker's wake model went with it. The worker fails closed, so every job leaves the room within about 75 ms, before it publishes an admission nonce. The BOX firmware therefore ignores every tap. The orphan sweeper deletes the empty-agent room about every 42 s, and that unexpected disconnect is the **black dog**. Behind it sits a second blocker: **the OpenAI voice credit is empty** (no billed session since 2026-09-16).
- **Telegram inbound: the bridge is up and polling, but replies fail.** A message today would get the reply `[bridge] (brain 500: Internal Server Error)`, not Narada. Most likely cause: the brain's Telegram session tries to resume Claude transcript `1121e781…`, which Claude Code's 30-day cleanup deleted. **Workaround: send `/new` in Telegram** (untested, since nothing was sent).
- **`narada doctor` is built:** 22 read-only checks with fakes-based tests. First live run: **7 red, 4 amber, 11 green.** The digest, the external dead-man's switch and the phone status card are designed in `docs/doctor.md`; none is enabled. *(2026-11-04: Suti rejected the external switch, since it needs an extra account. The digest and the card are built instead; see `docs/doctor.md`.)*

## Part 1a. The BOX: taps do nothing, and the black dog appears now and then

### Root cause (confirmed)

| Step | Evidence |
|---|---|
| 1. The model was moved off C: on 10-22 | Claude session `0ff9588b` (cwd `C:\Projects\gpu_tests`), 10-22 ~10:50Z: robocopy `/E /MOVE` of `~/.narada/wakeword\data` and `\output` to `D:\Archive\wakeword\…` ("move, not delete"), 48,007 files. `~/.narada/wakeword` mtime is **2026-10-22 20:59**. `D:\Archive\wakeword\output\narada\narada.onnx` is intact (99,190 bytes, dated 2026-08-07). |
| 2. Every BOX job bails before admission | host.log: `ERROR:narada-voice:wake model unavailable (set NARADA_WAKE_GATING=off to bypass): wake model missing: …\narada.onnx`, then `wake model unavailable — no session opened`. 326–338 times per 4-hour log, all 20 retained hours. Code: `worker.py:350-359` (running copy) calls `bail()` and `ctx.shutdown()` when `WakeGate()` raises FileNotFoundError. |
| 3. No nonce, so taps are dropped on the device | The admission nonce is only published from the admission loop (`worker.py:~1048`), which is never reached. Firmware `narada-box3/main/example.c:204-207`: `tap: no admission nonce yet — ignored`. The tap never leaves the BOX, so nothing shows in any PC log. |
| 4. Room recycling produces the black dog | LiveKit log: the agent joins for ~75 ms and leaves with `CLIENT_REQUEST_LEAVE`. About 40 s later the worker's room watchdog logs `room narada-body orphaned (device present, no agent) — recycling` and deletes the room (`SERVICE_REQUEST_DELETE`). Firmware `example.c:485-509`: a disconnect outside a session or recycle window is "unexpected", so it shows `FACE_OFFLINE`, the black dog. The BOX rejoins in ~0.4 s, so the dog flickers. Median cycle: **42 s**. |
| 5. The supervisor calls it healthy | The worker's health URL `:8792` answers 200, and the A3 alert DB shows `voice` healthy since 10-23 10:44. The LiveKit API (read-only ListParticipants) shows `narada-body` holding only `narada-box3` and no agent. |

### Hypotheses, ranked by evidence

1. **The missing wake model makes the worker fail closed** (confirmed; explains "can't tap" and the dog). Evidence: steps 1–5 above.
2. **The voice backend's OpenAI credit is empty** (confirmed; the next blocker). The feed has `voice-fault:credits` rows on 09-16, 09-17 and 10-13. `%LOCALAPPDATA%\narada\voice-fault.json` holds `{"code": "credits", …, "at": "2026-10-13T17:37:04"}`; the file is removed only by a successful session. The voice-minutes ledger's last billed day is **2026-09-16**. `voice-prefs.json` selects `gpt-live-1`, which is still OpenAI. So from 09-16 to 10-22 a tap was admitted and then failed with the credit chip; since 10-22 it has done nothing at all. The `rate_limit` errors Suti mentioned aren't in the retained logs (host.log keeps about 20 h).
3. **The running worker is uncommitted WIP** (risk, not cause). The supervisor runs `C:\Projects\prana` with the 2026-09-20 changes uncommitted: model selection through prefs, and the `gpt-live-1` "live" backend. The next admitted session runs a code path that has never been landed or reviewed.
4. **Ruled out:**
   - LiveKit: up, and the BOX joins in ~0.4 s.
   - Dispatch: a job is assigned within ~200 ms of every join.
   - BOX network: its LAN address answers ping in 3–6 ms.
   - Firmware: the ESP32 SDK 0.3.10 joins and publishes its mic track normally.

**Device logs:** not read. The BOX's USB serial line is COM3, and opening it can pulse RTS and reset the ESP32. That would count as a restart, so the evidence above is server-side plus the firmware source.

### Fix (not done: no changes outside the worktree)

1. `Copy-Item D:\Archive\wakeword\output\narada\narada.onnx C:\Users\admin\.narada\wakeword\output\narada\` (recreate the folders). `WakeGate()` is built per job, so the next dispatch, within about 45 s, picks it up with **no restart**. Taps then admit again.
2. Then either top up OpenAI, or carry on with presence plan W5 (the Claude-minded cascade). Until one happens, a tap admits and shows the honest "OpenAI credit is empty" chip, and nothing is billed.
3. Decide the fate of the WIP voice changes before (2) (W0).
4. Prevention: the doctor's `voice_box` and `box_dispatch` checks would have gone red at 21:00 on 10-22.

## Part 1b. Telegram inbound

**Is the bridge running and polling?** Yes.

- `narada_chat_bridge.py` has been pid 12376 since 2026-10-22 15:31, under the supervisor.
- It long-polls `getUpdates` every ~10 s: about 1,380 successful polls per 4-hour log, with one transient `NetworkError: Bad Gateway` at 11:11 today that recovered.
- Outbound works: the akhada tick sent its 22:00 nudge by Telegram.
- No inbound message has arrived in the retained 20 h of logs (zero `inbound chat_id=` lines).

**The last exchanges:** `~/.narada/heartbeat/chat-cycles/`. The last OK was **2026-09-17**; then `2026-10-22-160232` (10-23 02:02) and `2026-10-23-021707` (10-23 12:17), both `FAILED` with `(brain 500: Internal Server Error)`. The bridge relays failures as `[bridge] …`, so Suti saw that error text both times.

**Would a message from Suti today get a reply?** It would get a reply, almost certainly `[bridge] (brain 500: Internal Server Error)` again, not Narada.

**Root cause (high confidence, inferred; the 10-23 traceback has rotated out of host.log):**

1. The brain's session dir `brain/sessions/prana/telegram-<chat>` holds `native-session-id = 1121e781…`, last written **2026-09-17 18:23**. The backend is `claude` and the model `sonnet`.
2. That transcript no longer exists anywhere under `~/.claude/projects`. Claude Code prunes transcripts older than `cleanupPeriodDays`, which isn't set in `~/.claude/settings.json`, so the default 30 days applies. The oldest surviving transcript on the machine is dated 2026-10-05, which fits. 09-17 plus 30 days is 10-17, before the first failure.
3. A cold brain session builds `ClaudeSDKClient(resume=1121e781…)` (`brain/backend.py:59-77`). The CLI, given a missing resume id, exits 1 with `No conversation found with session ID: …`. I reproduced that with a dummy id in a scratch directory; no model call is made.
4. `chat_completions` (`brain/api.py:938-944`) only catches `ModelConflict` around `pool.get_or_create`, so the start failure escapes as FastAPI's **bare** "Internal Server Error". A failure *inside* a turn returns JSON `turn failed: …` instead, which is why the bare text points at session creation.
5. Nothing clears the stale id after a failure, so every new message repeats the failure. It failed before (02:02) and after (12:17) the brain's restart at 10-23 10:45, so the cause lives on disk, not in the process.

**Workaround for Suti:** send `/new`. It bumps the bridge's brain generation to `telegram-<chat>-g1`, a fresh session with nothing to resume. The doctor's test `test_inbound_after_new_uses_fresh_generation` covers this logic, but it hasn't been tried live.

**Code fix (not done):** in `SessionPool._create_locked`, when the resume target's transcript is missing, or `backend.start()` fails on resume, drop the resume and hand the conversation over from the brain's own `transcript.jsonl`. The mechanism already exists for backend switches (`handover_text`). Log it loudly, and return a JSON error naming the cause instead of a bare 500.

**Same problem elsewhere:** all **9 of 9** resumable brain sessions point at pruned transcripts, including the phone app's sessions (`brain_sessions`, amber). The app chat would hit the same 500 on its next use of an old session.

Also seen: the bridge starts with `drop_pending_updates=True`, so messages sent while it's down are discarded silently at the next start.

## Part 2. `narada doctor`

Code: `src/prana/doctor/` (`model.py`, `probes.py`, `checks.py`, `runner.py`, `digest.py`, `__main__.py`), `scripts/doctor.py`. Design and JSON shapes: `docs/doctor.md`.

```
python scripts/doctor.py [--write] [--json | --card | --digest] [--only ids] [--timeout S]
```

The doctor is read-only:

- sqlite opens with `mode=ro`;
- git runs only `rev-parse`, `rev-list` and `log`, with optional locks off and no fetch;
- one PowerShell call reads Task Scheduler;
- a short-lived in-process token signs two read-only LiveKit calls, and the secret never leaves the probe;
- every check has its own deadline, and a crash or timeout is reported AMBER, never green.

It writes `~/.narada/host/doctor.json` only with `--write`. Exit code 0, 1 or 2 means green, amber or red. A run takes about 1.5 s.

### First live run (2026-11-03 23:49, without `--write`)

| Status | Check | Reason (abridged) | Since |
|---|---|---|---|
| RED | Telegram inbound → reply | last exchange 10-23 12:17 FAILED (brain 500); session resumes pruned transcript 1121e781…, so the next message 500s too | 10-23 02:02 |
| RED | BOX voice: agent dispatch | 63 of 63 recent jobs bailed "wake model unavailable" before tap admission; room recycled every ~42 s (the black dog) | ~10-22 20:59 |
| RED | BOX voice worker | wake model missing; OpenAI credit empty (fault 10-13); last billed session 09-16 | ~10-13 |
| RED | Phone voice worker | registered, but OpenAI credit empty (shared backend) | ~10-13 |
| RED | Phone push subscriptions | no devices subscribed | ~09-08 |
| RED | smriti nightly | last night (11-02) failed at step **index**: numpy.core.multiarray failed to import | ~10-26 03:06 |
| RED | qmd recall index | newest indexed document 2026-09-07 (57 days); the nightly embed reports rc 0, so nothing new lands | 09-07 |
| AMBER | Brain sessions resumable | 9 of 9 point at pruned transcripts | |
| AMBER | BOX device | connected, but dropped 7× in 24 h (room recycles) | |
| AMBER | Narada scheduled tasks | Narada_Scene_Nightly 0x1; Daily Debrief exits 0 but sent the same 130-character text 3 nights running (a false green) | |
| AMBER | Secrets in host.log | host.log records the Telegram bot token | |
| GREEN | Supervisor (Narada_Host) | pid beating, up since 10-22 15:31 | |
| GREEN | Supervised components | 5 answer their health URLs; chat-bridge, sessions and akhada-brain have no probe | |
| GREEN | Brain :8811 | /health ok | |
| GREEN | Brain over tailnet :8443 | answers | |
| GREEN | Telegram bridge polling | long-polling, last poll 2 s ago | |
| GREEN | LiveKit media server | :7880 and tailnet wss :7443 answer | |
| GREEN | smriti logd + morning | logd alive; morning sent 11-03 | |
| GREEN | akhada check-in tick | 22:00 tick ok (Telegram) | |
| GREEN | Memory backup pushed | 1 unpushed (23:18, from this audit; see Disclosures) | |
| GREEN | Dead-man watchdog | ticking every 5 min, no restarts | |
| GREEN | Host alerts (A3 pager) | no open episodes; outbox empty | |

**Memory backups changed during the day.** The plan's "78 unpushed since 10-26" is no longer true: origin advanced to `c794b8a` at 22:56 today. Its reflog shows `fetch -q origin: fast-forward` at 23:10, so the backlog was pushed from elsewhere. `backup.py` still swallows push errors (it exits 0), which is how the backlog built up unseen.

### Designed, not enabled (`docs/doctor.md`)

- **Daily digest:** red only, at most once a day at 08:30, through `NOTIFY_LEVEL=error notify_suti.py`, the script `tell_suti` wraps. It names each failing check and the nightly's failing step, with since and next action. `compose_digest`, `digest_due` and `fingerprint` are built and tested; there is no sending code. `--digest` previews the text.
- *(Rejected by Suti on 2026-11-04: no extra accounts. Kept here as the record of what was proposed; the morning message is the heartbeat instead. See `docs/doctor.md`, "Why there is no external dead-man's switch".)*
- **External dead-man's switch: Healthchecks.io**, free Hobbyist plan. It alerts by email and through Healthchecks' own Telegram bot.

  | Check | Pinged by | Schedule | Grace |
  |---|---|---|---|
  | `narada-pc` | deadman.ps1, every tick | 5 min | 15 min |
  | `narada-host` | deadman.ps1: fresh beacon → success, stale → `/fail` | 5 min | 15 min |
  | `smriti-nightly` | nightly via cron_run: `/start`, then `/rc` | 03:00 | 60 min |
  | `smriti-morning` | morning via cron_run: `/rc` | 07:00 | 45 min |
  | `akhada-checkin` | cron_run: `/rc` | 11:00, 18:00, 22:00 | 30 min |
  | `narada-doctor` | hourly doctor run: red → `/fail` with the digest as body | hourly at :15 | 30 min |
  | `memory-backup` | backup.py, on a successful push only | period 1 day | 1 day |

  Setup steps for Suti (no account created):
  1. Create an account and project on healthchecks.io.
  2. Create a Ping key and save it to `%LOCALAPPDATA%\narada\healthchecks.key`.
  3. Add the Telegram integration (keep Email).
  4. Create the 7 checks with these slugs, time zone Australia/Brisbane.
  5. Say go; the pings land as one reviewed change: a `Send-HcPing` function in deadman.ps1, pings from cron_run.py, the smriti tasks routed through cron_run.py, a ping from backup.py on a successful push, and an hourly Narada_Doctor task.
  6. Rehearse: shorten `narada-pc`'s period, disable Narada_Deadman for 5 minutes, and confirm the alert arrives.
- **Phone status card:** `kind: "narada.status-card"`, version 1. It is self-contained and ordered worst first, with headline, counts, items (label, status, reason, since, action) and `stale_after_s` for the honest offline state. It's included in doctor.json and printed by `--card`. A brain route `GET /api/doctor/card` is proposed and must join the W4 route inventory.

### Tests

- New: `tests/test_doctor_checks.py` and `tests/test_doctor_runner.py`, with `tests/doctor_fakes.py` (an in-memory fake `Probes` plus a "healthy world" builder). **95 tests.** The healthy world is all green, and every check has red and/or amber cases built from today's real failures. Further tests cover crash and timeout isolation, the atomic `--write`, the card shape, redaction, the digest, the CLI, and the real probes' safety: a read-only sqlite insert raises, a dead port is bounded, and the LiveKit secret never appears in errors.
- Full prana suite in the worktree, with `PYTHONPATH=src` because the editable install points at the main copy: **586 passed, 1 skipped.**
- ruff is clean on all new files.

## Other findings worth fixing

1. **Liveness isn't function.** The A3 pager watched the BOX worker bail on more than 2,000 jobs and the brain 500 every Telegram turn, and stayed "healthy". Wire the doctor in (the hourly `narada-doctor` ping) before adding more liveness probes.
2. **host.log keeps only about 20 hours.** Five 10 MB rotations fill up, mostly with LiveKit INFO lines and this bail loop (~330 cycles every 4 h). Any incident older than a day loses its evidence; the 10-23 brain traceback is already gone. Lower LiveKit's log level, or give components their own logs.
3. **The bot token is in host.log** (the bridge's httpx INFO logging). Fix the log level.
4. **qmd recall has been frozen since 09-07.** The nightly's `recall_refresh` logs only `qmd embed`'s return code (0), and `qmd status` says "Updated 57d ago". Run `qmd update` by hand and read its output.
5. **The Daily Debrief is a false green.** It exits 0 after sending its ~130-character failure notice every night (W3's "command line is too long").
6. **Narada_Scene_Nightly fails nightly** (0x1: "Unfinished scene request 2026-09-12; refusing to stack another night").
7. **`Narada_Host` last ran 10-23 10:41 with result 0x1** and doesn't own the running host (pid 16896, started 10-22 15:31). It's probably a refused second start, since the lock was held, so it's benign. But the deadman, which restarts the task, is the only path back if the host dies.
8. The deadman can page only while `~/.hermes/.env` exists. The doctor checks for it.

## Disclosures: what this audit touched

- **A secret was printed.** A redaction regex of mine missed the hyphen in `narada-key`, and the **LiveKit API secret was printed into this session's local transcript** while I inspected `livekit.yaml`'s shape. It wasn't written into any committed file. The W4 survey had already exposed it once. **Rotate the LiveKit key/secret.** Note that the BOX connects with a pre-minted token (`~/.narada/esp32/box3-livekit-token.txt`), which rotation invalidates, so mint a new BOX token at the same time.
- **A memory snapshot commit.** The `claude -p --resume <dummy>` probe fired the user-level SessionEnd hook (`backup.py --tag session-end --push`). It made routine snapshot commit `9a87cc9` in `~/.narada`; the push was cancelled. The next session-end backup will push it.
- **`qmd status` was run once.** It opens the index sqlite; my own reads used `mode=ro`.
- **No restarts, no config changes, no messages, no paid API calls.** COM3 was not opened. `doctor.json` was **not** written on the live system; `--write` is exercised in tests against a temp directory.
- **Commits:** on `doctor` only. Not merged, not pushed. The main working copy (`briefing`, with the September WIP) was not touched.
