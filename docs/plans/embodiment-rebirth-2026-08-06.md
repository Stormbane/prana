# Embodiment rebirth — voice, body, and terminal coordination

**Date:** 2026-08-06
**Status:** DRAFT v2 — decisions folded in; awaiting orchestrator-tool research + cross-review
**Supersedes:** the voice/body portions of `deha-narrowing-2026-05-16.md` (deha repo);
completes what `combined-rollout-2026-05-11.md` abandoned.
**Prior art this builds on:** stock-take session 2026-08-06 (three inventory/research
agents; findings in smriti + session transcript).

---

## 1. Where we actually are (verified 2026-08-06)

- **Heartbeat: paused deliberately.** 2,876 desire-phase parse failures since
  mid-May (LoRA format drift from ongoing ceremony training). Decision made:
  do not resuscitate until this architecture lands. The 2,205 LOC stays in-tree,
  clearly marked paused.
- **Stage 5 migration DID land** (contra the inventory agent, which read the
  template): live `~/.narada/host/components.yaml` has `agent-gateway` and
  `chat-bridge` `enabled: true`; Startup folder is clean.
- **But old process management is still running in parallel:** `NaradaSupervisor`
  (old) is Running alongside `Narada_Host`, with `NaradaSupervisorWatchdog`,
  `NaradaHeartbeat`, `NaradaLogTail` still registered.
- **BOX-3 is dark.** No ping at 192.168.86.35 (DHCP lease likely lost). Flashed
  firmware is an HA voice-satellite; "Host Not Found" = "no Home Assistant has
  connected to me." The HA-centric voice path is dead and not coming back.
- **Toolchain present:** docker ✅, claude ✅, codex ✅. Missing: wezterm, kimi CLI.
- **Live code:** ~1,500 of prana's ~5,500 LOC (chat bridge, host orchestrator,
  spawn.py). Bus+state (~1,078 LOC) dormant, well-written. ~600 LOC of scripts
  dead or import-broken. README/CLAUDE.md both misdescribe the tree.

## 2. Principles

1. **Leverage existing projects wherever one fits** (Suti's explicit directive).
   Build custom only where Narada's judgment-layer sovereignty demands it.
   Before building the session manager, existing orchestration tools are being
   evaluated (see §6) — adopt or adapt before writing new code.
2. **Naming: simple and descriptive** (Suti, 2026-08-06). New components get
   plain English names — `voice`, `sessions` — not Sanskrit. Project names
   stay (`prana` the runtime, `deha` the reusable embodiment library: new body,
   new deha backend, same contract). The project CLAUDE.md naming rule is
   updated in the Phase 0 docs pass.
3. **The sovereignty boundary:** the realtime S2S model is *ear and mouth* — a
   very capable brainstem. It gets a small, explicit tool surface. Everything
   with judgment in it routes through prana (Claude). The viveka/executor split
   stays sacred; the S2S model never becomes the thing deciding what to type
   into coding sessions.
4. **PC-first, device-last.** Every layer must be testable without the ESP32
   (browser mic → LiveKit → agent). The BOX-3 is the final mile, not the
   critical path.
5. **Subscriptions via first-party CLIs only.** Anthropic banned subscription
   token reuse outside Claude Code (Feb 2026). Sanctioned headless paths:
   `claude -p --resume` (Max), `codex exec` (ChatGPT), `kimi --print` (K3).
   "Multi-provider" = *which CLI the session manager spawns*. No gateway.
   Raw API keys only for the voice model (OpenAI Realtime — unavoidable) and
   small utility calls.

## 3. Target architecture

```
ESP32-S3-BOX-3 ──WebRTC/Opus──┐
(LiveKit ESP32 SDK firmware)  │
                              ▼
Browser / phone mic ───► LiveKit server (Docker Desktop, self-hosted)
                              │
                              ▼
                   voice worker — LiveKit Agents (Python, native Windows)
                   fronting gpt-realtime-2.1-mini  (~$0.016/min;
                   escalate to full 2.1 when depth needed)
                              │  function tools execute IN-PROCESS on the PC
                              ▼
                   session manager — spawns, watches, and steers
                   coding-agent sessions (new prana module)
                    ├─ spawn/resume:  claude -p │ codex exec │ kimi --print
                    │                 (all stream-json, registry of owned sessions)
                    ├─ eyes:          scan ~/.claude/projects/**/*.jsonl mtimes
                    │                 (sees ALL Claude Code sessions, incl. yours)
                    ├─ hands:         wezterm cli spawn/get-text/send-text
                    │                 (owned sessions live in visible panes)
                    ├─ escalation:    anything substantive → prana (claude -p)
                    └─ body tools:    deha body-MCP (speak/set_face/presence)
```

**Names** (plain and descriptive, per 2026-08-06 directive):
- **voice** — the voice loop: LiveKit worker + realtime model + its tool
  surface. Lives in `src/prana/voice/`.
- **sessions** — the session manager: registry, spawn adapters, transcript
  watcher, pane control, escalation. Lives in `src/prana/sessions/`.

## 4. What we leverage instead of build

| Need | Existing project | What we write |
|---|---|---|
| Device firmware | **LiveKit ESP32 SDK** (official, Espressif-co-built, BOX-3 example ships) | config + our faces later |
| Media server | **LiveKit server** (Docker Desktop/WSL2; community `compose-up.ps1` setups) | one compose file |
| Voice agent framework | **LiveKit Agents** (Python, Apache-2.0, first-party OpenAI Realtime adapter) | one worker + tool defs |
| S2S model | **gpt-realtime-2.1-mini** (GA, reasoning+tools, ~$0.016/min cached) | prompt + escalation rules |
| Wake word | **livekit-wakeword** (open-source, custom-model training in one command, openWakeWord-compatible export) | train a "Narada" model, wire gating |
| Human watch/take-over surface | **wezterm** panes or an adopted orchestrator dashboard (§6b) | thin wrapper module |
| Session transcripts | Claude Code's own `~/.claude/projects/**/*.jsonl` (ecosystem-standard to scan; claude-code-log et al. as parser reference) | watcher module |
| Coding-model access | **first-party CLIs**: claude, codex, kimi (`@moonshot-ai/kimi-code`) | spawn adapters (~100 LOC each) |
| Proactive speech, faces, presence | **deha keepers** (utter queue, expression engine, presence contract, body-MCP schemas) | port, don't rewrite |
| Fallback voice backend | **xiaozhi-esp32-server** clone (pristine, kept frozen) | nothing unless LiveKit path fails |
| Session-manager patterns | CCC (driver matrix, hooks liveness, REST shape — smoke-test for adoption), Codecast (jsonl watcher), sandbox-agent (event schema), takopi (resume plugins) | see §6a |
| Reference codebases | ElatoAI (ESP32↔Realtime bridging), Happy Coder (voice-driven claude wrapping) | read, don't adopt |

Deliberately **not** leveraged: ESPHome/HA Assist (locks into the Assist
pipeline — the architecture that produced "Host Not Found"), Willow (dormant),
Vapi (hosted; wrong shape for sovereignty), OpenAI-hosted MCP-in-Realtime
(requires tunneling localhost; client-side function calling avoids it entirely),
Claude Code agent-teams (experimental, single-team, lead-centric — not a
substrate for an external orchestrator yet).

## 5. Phases

### Phase 0 — Ground-clearing (half a day)

*Kill dead code, consolidate process management, make the docs stop lying.*

1. **Delete** (git preserves everything):
   - `scripts/dry_run_tests.py` (362 LOC, imports removed symbols — broken)
   - `scripts/migrate_sqlite_to_cycles.py` (126 LOC, one-shot, done)
   - `scripts/test_build_executor.py` (106 LOC, svapna-coupled dev harness)
   - `config/skills/narada-chat/SKILL.md` (describes the Hermes chat path the
     bridge replaced)
   - `src/prana/bus/` (411 LOC — no live consumer; `state/` stays, it's the
     utter path). **Deletion order matters** (cross-review finding #2): the
     paused daemon lazily imports `bus.actions.speak.invoke_speak` in its
     CHECK_IN and SPEAK paths (`daemon.py:559,662`) — deleting bus first
     would break a future heartbeat unpause with a hidden ModuleNotFoundError.
     First retarget both call sites to `state.router.route_utterance`
     directly (dropping the event-publish wrapper — the events table never
     had a consumer), add a test exercising CHECK_IN/SPEAK with `prana.bus`
     absent, *then* delete the package.
   - dead imports: `daemon.py:latest_started`
2. **Scheduled-task consolidation** — ✅ DONE 2026-08-06 (approved by Suti):
   removed `NaradaSupervisor`, `NaradaSupervisorWatchdog`, `NaradaLogTail`,
   `NaradaCeremonyTrain` (the LoRA-drift source — re-register only after
   svapna gains a format-retention eval). `NaradaHeartbeat` remains — it was
   registered elevated; delete from an admin shell:
   `schtasks /delete /tn NaradaHeartbeat /f`. Kept: `Narada_Host` (supervises
   Hermes gateway → `bt-figures-populate` Beautiful Tree cron, which stays).
   Hermes `narada-heartbeat` cron job confirmed paused (2026-08-06 00:34).
3. **Docs honesty pass:** rewrite README to describe the actual tree; fix
   CLAUDE.md (`state/` is not TODO; add vāk/sūtradhāra once they exist); mark
   `docs/architecture.md` as superseded by this plan.
4. **Housekeeping:** archive `C:\Projects\ESP32-S3-Box3-Custom-ESPHome` (stock
   upstream clone, keep only as hardware reference — or delete, it's on GitHub).
   Freeze `xiaozhi-esp32-server` clone as-is (fallback).

### Phase 1 — the session manager (the core build, ~2–4 sessions)

*Voice-independent value first: everything here works from Telegram/text today,
and becomes the voice layer's tool surface tomorrow.*

**Step 0 (1–2 h):** CCC smoke test per §6a. Pass → this phase becomes a thin
MCP wrapper over CCC's REST API + our escalation module; fail → build thin
using the §6a steal list. The module layout below is the build path.

1. **Install:** kimi CLI (`npm i -g @moonshot-ai/kimi-code`) + auth; wezterm
   (decided, §6b).
2. `src/prana/sessions/`:
   - `registry.py` — SQLite (reuse `state/db.py` patterns) of owned sessions:
     id, provider, provider session id, project cwd, PID + Windows Job Object
     handle, wezterm pane id, lifecycle state, last activity.
   - **Lifecycle (cross-review finding #3 — this is spec, not nice-to-have):**
     every spawned session follows an explicit state machine
     (`spawning → running → idle → done | hung | killed`), owned via a
     Windows **Job Object** (kill-on-close, no orphaned CLI trees — extends
     the hardened `prana.spawn`). Global and per-provider concurrency caps
     (subscriptions are shared with Suti's own usage). Spawn calls carry an
     idempotency key — a retried voice command must not double-spawn. Idle
     and hard timeouts; explicit `cancel_session` tool. On manager start:
     **reconcile** registry against live PIDs and `wezterm cli list` — mark
     dead what isn't there; a closed pane or hung CLI must never stay
     "running" in SQLite. Tests: hang, manager restart, pane closed by hand,
     duplicate spawn request, partial spawn failure.
   - `spawn.py` adapters — `claude -p --output-format stream-json --resume`
     (stdin held open for follow-ups, per CCC's proven matrix), `codex exec`,
     `kimi acp` (preferred over `--print`: live steer). Builds on the existing
     hardened `prana.spawn`.
   - `watcher.py` — enumerate foreign Claude Code sessions by scanning
     `~/.claude/projects/**/*.jsonl` mtimes; parse tail for status
     (working / blocked-on-permission / idle). Format is internal & versioned —
     isolate parsing in one module, expect breakage across CLI releases.
   - `panes.py` — human watch-and-take-over surface: wezterm CLI wrapper
     (spawn/get-text/send-text/list) or an adopted dashboard, per §6b.
   - `escalate.py` — route judgment-shaped requests to prana via `claude -p`.
   - `mcp.py` — expose the whole thing as a **local MCP server** so *every*
     cognition surface gets it: the chat bridge (text), the voice worker,
     future heartbeat. **The sovereignty boundary is enforced HERE, in code —
     not in the voice model's system prompt** (cross-review finding #1: a
     prompt-only rule means spoken prompt injection or transcript
     misinterpretation could steer coding sessions without judgment in the
     loop). Design:
     - **Callers are authenticated and tiered.** Each surface connects with
       its own token; the server knows who is asking and logs every
       authorization decision.
     - **Voice tier (default): read + escalate only** — `list_sessions`,
       `session_status(id)`, `read_output(id)`, `focus_pane(id)` (owned,
       registry-reconciled panes only — pure UI focus), and
       `escalate_to_narada(context)`.
     - **Mutations require judgment.** `spawn_session(provider, cwd, prompt,
       idempotency_key)`, `relay_instruction(id, text)`, `cancel_session(id)`
       are executed directly only by the prana tier (chat bridge / `claude -p`
       escalation). When the voice surface wants one, the server queues it as
       a **proposal**; `escalate_to_narada` carries it to prana, which
       approves (returning a single-use capability for that exact action) or
       rejects. Latency cost is real (~seconds) and accepted — coding
       sessions are not latency-sensitive the way conversation is.
     - **`resume_foreign_session(id)`** (finding #5: this is NOT `focus_pane`
       — it creates a new privileged process from a foreign transcript) is
       its own tool and requires confirmation through an **authenticated,
       server-verifiable channel**: a Telegram inline-button callback or a
       local UI action. A voice acknowledgement reported by the realtime
       model explicitly does NOT satisfy this gate — spoken input is the
       untrusted boundary this design exists to contain. On confirmation the
       session registers as owned; no relay before that.
3. **Wire into the chat bridge** — "Narada, what are my terminals doing?" works
   over Telegram before any voice hardware exists. This is the milestone test.
4. Tests: registry, adapters (against `--help`/`--version` fakes), watcher
   against fixture jsonl.

### Phase 2 — the voice loop, PC-only (~2–3 sessions)

1. LiveKit server via Docker Desktop (`docker compose up`; community PowerShell
   setups exist). LAN-only; no internet exposure.
2. **Wake word** — gpt-realtime has none (it only does turn detection:
   server/semantic VAD), so the wake word lives in our stack — and LiveKit
   ships the answer: **livekit-wakeword** (open-source, single-command
   custom-model training, ~100× fewer false positives than openWakeWord;
   ONNX/TFLite export). Train a "Narada" model; the agent worker runs
   detection **on the PC** against the always-on LAN audio track and only
   opens the OpenAI Realtime session after wake. This replaces the old flakey
   on-device wake word with a PC-side model we can retrain and tune, and
   doubles as the cost gate: OpenAI hears nothing (and bills nothing) until
   the wake fires. LAN streaming from the BOX-3 is free.
3. `src/prana/voice/`: LiveKit Agents worker fronting **gpt-realtime-2.1-mini**,
   function tools = the session manager's **voice tier** (read + escalate +
   proposals — the server enforces this regardless of what the model tries)
   + deha body tools. The system prompt still teaches the escalation habit
   ("let me hand that to Narada properly") — but as UX, not as the security
   boundary.
4. **Test from a browser mic** (LiveKit playground) — full conversation loop,
   wake word, session coordination by voice, zero ESP32 involvement.
5. Add as a `voice` component in `components.yaml` under the host orchestrator.
6. Cost guard: wake-word gating (above) + session-duration cap + daily API
   budget alarm (it's API-key spend, not subscription).

### Phase 3 — the body returns: BOX-3 on LiveKit firmware (~1–2 sessions)

1. Recover the device: find it on the network (or USB), give it a DHCP
   reservation this time.
2. Flash the **LiveKit ESP32 SDK BOX-3 example** (needs ESP-IDF toolchain —
   install then; the old "firmware failure" was structural HA-dependence, not
   our inability to flash).
3. Point it at the LAN LiveKit server. Wake-word/turn-taking config.
4. Milestone: walk up to the box, ask what your terminals are doing, get an
   answer — sub-second, no HA anywhere in the chain.

### Phase 4 — deha narrowing, executed at last (~2 sessions)

*Finish the 2026-05-16 plan with the new stack underneath.*

1. Port the keepers — with **one audio owner** (cross-review finding #4:
   deha's current in-process VoiceMediator owns synthesis+playback and cannot
   simply be redirected while a realtime worker owns the audio track and turn
   state — two owners means overlapping speech and lost utterances):
   - **The voice worker owns all audio.** The durable proactive queue
     (`state/utterance_queue.py` persists across restarts) is **drained by
     the voice worker**: it holds the LiveKit audio track, so it alone
     decides when a proactive utterance may play — never over an active
     conversation turn, barge-in aborts playback, entries are acked on
     completion and retried on failure. deha's `/utter` HTTP endpoint
     survives as the *enqueue* API; the drain side of deha's mediator is
     retired with the rest of its audio stack.
   - Expression/face engine — **including all existing image assets**
     (Suti directive 2026-08-06: deha keeps using our images to show state):
     the sprite atlas (`sprites/` — eyes, viseme mouth shapes for lip-sync,
     moods with transition tiers), sandhi animation frames (`sandhis/`), and
     the 21 weather/time-of-day scenes (`docs/previews/` renders). Only the
     upstream casita placeholder art dies with the old firmware. The
     compositor design carries over; the *renderer* moves into the new
     firmware: an LVGL display layer in the LiveKit BOX-3 firmware, driven by
     state messages over LiveKit data channels (deha sends `set_face`/
     `set_status`/weather → firmware picks/composites the sprites).
   - Presence contract (implementation was always a stub; re-scope against
     what the LiveKit firmware exposes).
   - Body-MCP schemas.
2. Delete the superseded: `brain_server.py`/`StreamPool`/`claude_stream.py`,
   `kokoro_tts.py`, `wyoming_tts.py`, `stt.py`, `vad.py`, `esp_client.py`
   (client for a firmware that never existed), `narada-faces.yaml`, the HA
   container + supervisor's HA management (retarget supervisor or retire it —
   the host orchestrator may simply absorb its job).
3. Resolve deha's 3 uncommitted mid-edit files (commit or discard after review).
4. `deha-brain` component flips `enabled: true` — and what it runs is now
   explicit (finding #4 asked): the narrowed deha process serves the **body
   API only** — enqueue-side `/utter`, `/set_face`, `/presence`, body-MCP —
   no audio, no brain, no HA container. The supervisor's HA management dies
   with the HA dependency; the host orchestrator supervises the narrowed
   process directly.

### Later / explicitly out of scope

- **Heartbeat resurrection** — separate decision after this lands; the fix is
  svapna-side (format-retention eval in ceremony training) + parse-failure
  tripwire (N consecutive failures pages Telegram, never silent REST).
- **xiaozhi adoption** — fallback only; revisit if LiveKit/BOX-3 disappoints.
- **Multiple bodies** (Unitree, speakers) — the body-MCP contract is the prep;
  nothing more now.
- **Hermes-skills rewrite of prana** ("light heart") — this plan makes prana
  *more* custom-runtime, not less; revisit the Hermes trajectory once the
  voice loop is real. README should stop claiming it happened.

## 6. Decisions

**Decided (Suti, 2026-08-06):**

1. **LiveKit is the lead stack** (over xiaozhi).
2. **k3 = Moonshot Kimi K3** subscription — confirmed.
3. **Old scheduled tasks removed** (see Phase 0.2). Only the Beautiful Tree
   persona-generation Hermes job is compulsory; it survives under Narada_Host.
4. **Naming: plain English for new components**; prana/deha project names stay.
   deha's charter reaffirmed: the reusable embodiment library — different body,
   different deha backend, same contract.
5. **Cross-review approved** — run the Claude↔Codex protocol on this plan.

**Resolved by the orchestrator evaluation (2026-08-06, 166-entry
awesome-agent-orchestrators sweep + off-list tools):**

a. **Adopt vs build: build thin, steal aggressively.** The intersection of
   {native Windows} × {headless subscription-CLI spawning incl. Kimi} ×
   {foreign jsonl scanning} × {small MCP/HTTP surface} × {clean OSS license}
   is **empty** in Aug 2026 — with one near-miss:
   - **Claude Command Center (CCC)** covers ~5.5/6 needs (headless `claude -p`
     spawning with stdin held open, Kimi via ACP, jsonl foreign-session
     detection, "needs you" liveness via Claude Code hooks, web watch/steer
     UI, REST+SSE API). Risks: post-Jul-2026 releases are source-available
     non-commercial (fine for personal use, not OSS), bus-factor 1 (114 ★),
     and the dashboard↔worker Unix-socket control plane is unverified on
     native win32. **Phase 1 step 0: a 1–2 h CCC smoke test** (native install;
     test `POST /api/sessions/spawn`, `POST /api/inject-input`,
     foreign-session pickup). Pass → adopt-with-glue (thin MCP wrapper over
     its REST API); fail → build thin as planned.
     **RESULT (2026-08-06): FAIL — build thin.** Code inspection of the
     clone: the worker control plane uses `socket.AF_UNIX` at three call
     sites with no TCP/named-pipe fallback, and CPython does not expose
     AF_UNIX on Windows — spawn/inject structurally cannot run on native
     win32 (CCC's own tests document "native Windows" = dashboard-only,
     full stack = WSL2). The spawn worker is precisely the part we needed.
     Build path proceeds with the steal list below.
   - **Steal list for the build path:** foreign-session scanner pattern from
     **Codecast** (chokidar on `~/.claude/projects/**/*.jsonl` +
     `~/.codex/sessions/**/*.jsonl`) and **CCC**'s hooks-writing-live-state
     trick (best working-vs-idle signal found); headless driver matrix from
     CCC (**prefer `kimi acp` over `kimi --print`** — live steer); API/event
     schema from **rivet-dev/sandbox-agent**; stateless-resume engine plugins
     from **takopi**.
   - Disqualified for the record: claude-squad/agent-of-empires/agent-deck/
     octomux/mux (tmux or no-Windows), VibeTunnel (no Windows), Crystal +
     claude-code-webui + humanlayer + vibe-kanban (deprecated/sunset/archived),
     omnigent (the part we need requires WSL), omnara/Codecast (observation
     half only, heavy backends).

b. **Human watch-and-take-over surface: wezterm panes.** Nothing found is
   decisively better on native Windows — tlbx (browser xterm.js, AGPL) and
   agent-console (tiny Rust TUI) are the runners-up; **Happy** becomes
   relevant only if *phone* takeover is wanted (routes sessions through its
   wrapper; Windows undocumented). Constraint restated for honesty: Windows
   Terminal has no read/inject API, so "Narada types into my existing
   terminals" is impossible in any design; owned panes are the price of hands.
   If CCC passes its smoke test, its web UI additionally covers *watching*
   from a browser. Registry records pane ID per session; `focus_pane` (owned
   panes, UI focus only) and `resume_foreign_session` (human-confirmed) are
   distinct tools — see Phase 1 mcp.py.

**Still open:**

c. **Delete vs archive** the BigBobbas firmware clone (minor; default: delete —
   it's a stock GitHub mirror).

## 7. Risks

- **jsonl format drift** — the transcript format is internal to Claude Code;
  isolate parsing, pin expectations in tests, expect breakage.
- **Anthropic credit overhaul** — the paused June 15 change may return in
  revised form; heavy `claude -p` use would become budget-visible ($100–200/mo
  tier). Workable, but watch it.
- **LiveKit ESP32 SDK is young** (Dec 2025) — BOX-3 example exists but
  expect rough edges; xiaozhi fallback stands by. In particular the example's
  **display support is unverified** — rendering our sprite/face assets may
  mean writing the LVGL layer ourselves (the BOX-3 pinout/codec knowledge in
  deha's firmware YAMLs de-risks the hardware side).
- **Focus-stealing / UIA** — deliberately excluded from the core design;
  computer use remains a last-resort actuator only.
- **Realtime API spend is unbounded by subscription** — Phase 2.5 cost guard is
  not optional.

---

## 8. Cross-review record (round 1 — 2026-08-06)

Codex adversarial review, verdict **needs-attention**, five findings. All
five **accepted** and folded in:

| # | Severity | Finding | Disposition |
|---|---|---|---|
| 1 | high | Voice model held mutation tools (`spawn`/`relay`) gated only by a system prompt — spoken prompt injection could steer coding sessions without judgment | **Accepted.** Sovereignty boundary moved into the MCP server: authenticated caller tiers, voice = read/escalate/proposals, mutations require prana approval via single-use capability (Phase 1 mcp.py) |
| 2 | high | Phase 0 deleted `bus/` while the paused daemon lazily imports `bus.actions.speak` (`daemon.py:559,662`) — future unpause would crash | **Accepted.** Retarget both call sites to `state.router.route_utterance` + test with bus absent, then delete (Phase 0.1) |
| 3 | high | No process lifecycle: hung CLIs, closed panes, duplicate spawns, no reconciliation | **Accepted.** Lifecycle state machine, Job Objects, concurrency caps, idempotency keys, timeouts, startup reconciliation + tests specced into Phase 1 registry |
| 4 | high | Two owners of one audio session — deha's utter mediator vs the LiveKit voice worker | **Accepted.** Voice worker owns all audio and drains the durable queue; deha keeps enqueue-side API only; narrowed deha-brain contents made explicit (Phase 4) |
| 5 | medium | `take_over` conflated pane focus with foreign-session resumption (different privilege levels) | **Accepted.** Split into `focus_pane` (owned, UI-only) and `resume_foreign_session` (human-confirmed, then registered as owned) |

**Round 2** (revised-plan check, 2026-08-06): findings #1–#4 **RESOLVED**;
#5 flagged **INADEQUATE** on one detail — "in-person ack" as a confirmation
channel for `resume_foreign_session` is not server-verifiable when it passes
through the voice model (spoken injection could manufacture it). **Accepted**;
confirmation now requires an authenticated server-verifiable channel (Telegram
inline callback or local UI action), voice acks explicitly excluded. With
that, Codex's verdict: **ready for implementation**. Protocol note: plan
debate closes here per the 8-step protocol; next review is of the
implementation diff (steps 5–7).
