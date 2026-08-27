# Resilience and reach — hardening the runtime, giving the voice its senses

**Date:** 2026-08-27 (the evening the body came back after a multi-day outage)
**Status:** DRAFT v3 — cross-review complete (round 1: 7/7 accepted;
round 2: 3/3 accepted, plan debate closed). Awaiting Suti's ratification
of §6 decisions; execution-ready on his go.
**Parent plans:** `embodiment-rebirth-2026-08-06.md` (architecture, ratified),
`milestone2-face-and-context-2026-08-15.md` (M2 spec, execution-ready),
`presence-roadmap-2026-08-15.md` (F1–F12 horizon, draft).
Where a feature is already specced in M2, this plan schedules it and adds
only what is new; it does not re-spec.

---

## 0. What prompted this plan

The BOX-3 sat offline for days. Root cause: Docker Desktop autostarts,
its WSL integration with the (independently broken) Ubuntu distro fails,
Docker dies with an error dialog, and the LiveKit server dies with it.
Every supervised component kept running; the one unsupervised link in the
chain was the one that failed. Meanwhile the voice worker's health probe
returned 200 the whole time, so nothing acted and nothing paged Suti.

Fixed live on 2026-08-27: Docker's Ubuntu integration disabled;
`livekit-server` v1.13.6 now runs as a **native Windows binary**
(`~/.narada/host/bin/livekit-server.exe`, config
`~/.narada/host/livekit.yaml`, same keys/ports — box and worker unchanged).
It is currently a **manual process**: it does not survive a reboot. That
gap is Workstream A's first item.

Also observed the same evening: `smriti_read` failing with "database is
locked" (seven concurrent `smriti.mcp_server` processes, four leaked by a
single codex parent), while `smriti_write` worked.

## 1. Principles (inherited, restated once)

1. **The viveka split is sacred.** Judgment-shaped mutations route through
   prana; the voice tier is read/escalate/proposals, enforced in code.
2. **Never silent.** A fallback must never masquerade as a legitimate
   action; a dead or degraded component must be *visible* — on the face,
   in the log, and (new in this plan) on Suti's Telegram.
3. **The open voice surface is untrusted** (anyone in earshot). Tiering
   per M2 §2: shareable pack always; personal tier only after verified
   tap; email never on the open voice surface.
4. **Cost honesty.** Subscription CLIs for cognition; deterministic local
   loops free; paid realtime API only inside admitted sessions, capped
   fail-closed. New tools in this plan (search, music) must state their
   spend model explicitly.
5. **Leverage before build.** Native binaries and existing modules
   (state router, utterance queue, spawn hardening) before new code.

---

## 2. Workstream A — resilience (do first, ~1 session)

### A1. LiveKit becomes a supervised component  ✅ prerequisite for everything

- `components.yaml` gains:

  ```yaml
  - name: livekit
    description: "LiveKit media server - native binary (no Docker)"
    enabled: true
    command:
      - ${HOME}\.narada\host\bin\livekit-server.exe
      - --config
      - ${HOME}\.narada\host\livekit.yaml
    restart_grace_s: 5.0
    health_url: http://127.0.0.1:7880/
  ```

- The `voice` component gains `wait_for_url: http://127.0.0.1:7880/` so a
  cold boot brings components up in order instead of burning the worker's
  16-retry budget against a server that isn't up yet.
- Cutover procedure (ordering matters — a double-start is a port
  conflict): edit yaml → stop the manual `livekit-server.exe` → restart
  `Narada_Host` → verify worker re-registers and the box rejoins.
- Docker Desktop: `AutoStart: false` in `settings-store.json`. Docker is
  no longer in Narada's chain at all; it becomes a tool Suti starts when
  *he* needs it.
- The `config/livekit/docker-compose.yml` stays in-tree, marked
  superseded in a header comment (it documents the config's provenance).
- `scripts/livekit-up.ps1` is **rewritten, not deleted** (cross-review
  #7): it becomes the emergency native-start script — start the binary
  manually with the same config — and the documented rollback path.
- **Preflight before touching the working process (cross-review #7):**
  validate the edited `components.yaml` parses and the component
  resolves (`prana host check` or equivalent dry-run), the binary and
  config file exist and are readable by the task's account, port 7880/
  7881/UDP-range availability logic is understood (the manual instance
  still holds them — the check is on config, ACLs, and task state, not
  a trial bind), and the `Narada_Host` scheduled task is Running. Only
  then: stop manual instance → host restart → **timed verification**
  (worker re-registered within 2 min, box rejoined within 6 min).
- **Rollback is atomic and port-safe (round-2 fix — naively starting
  the emergency script while the supervised component still holds the
  ports would double-start LiveKit inside the very outage it's meant
  to fix):** on verification failure, the sequence is (1) set
  `enabled: false` on the livekit component / revert the yaml,
  (2) restart `Narada_Host`, (3) **verify the supervised
  livekit-server PID is gone and 7880/7881/UDP range are free**,
  (4) run the emergency start script — which itself **refuses to start
  if any livekit-server process exists or 7880 is bound**, (5) verify
  the restored server answers and the box rejoins before declaring
  rollback complete. Docker autostart is flipped off only after the
  forward verification passes.

**Acceptance:** reboot the PC (when Suti allows) → box returns to sleep
face with no human action. Kill `livekit-server.exe` by hand → host
restarts it within its grace window → box reconnects within its backoff.

### A2. Health probes must tell the truth

The voice worker served 200 on :8792 for days while it could not reach
LiveKit. Fix the semantics: the probe returns 503 unless the worker has
a **live registered connection to LiveKit right now**. The existing
"503 on lost connection" claim in `components.yaml` becomes true. Add a
test that fakes a dropped connection and asserts the probe flips.

While in there: the initial-connect retry loop (16 retries ≈ 2 min, then
exit → host restart → repeat) is fine as a recovery loop, but it is
exactly the "flapping" state A3 must page about.

### A3. The host pages Suti when a component is sick

New small module `src/prana/host/alerts.py`. **Revised per cross-review
#3** — the round-1 trigger conditions ("unhealthy > 5 min", "restart
budget exhausted") did not correspond to actual supervisor state: the
supervisor kills after 3 failed probes and *resets health counters on
respawn*, keeps only a 3-exit/60 s deque, and has a 5-min cooldown, not
a permanent give-up. The design is therefore **transition-based with
durable history**, not threshold-polling:

- The supervisor emits explicit lifecycle transitions (spawned,
  exited(rc), **health-probe-failed, health-probe-succeeded**,
  health-fail-termination, cooldown-entered) to an alert state machine
  that keeps **monotonic per-component history in `~/.narada/state.db`**
  — surviving respawns AND host restarts. A per-component
  **healthy-since timestamp is persisted** on probe success (round-2
  fix: probe results previously only reset an in-memory counter, so
  neither recovery nor timeout could be derived from transitions
  alone).
- **Deadline sweep (round-2 fix):** time-based transitions ("still not
  healthy 30 min after episode start", "healthy ≥ 5 min → close
  episode") cannot arrive as events, so a durable sweep evaluates all
  open episodes against persisted timestamps — at host startup and
  every 60 s thereafter. An episode that crosses its alert deadline
  while the host itself was down alerts on the first sweep after
  restart.
- Alert transitions: (a) ≥ 3 health-fail terminations for one component
  within a rolling 30 min (this is derivable: each is an explicit
  event, history is durable); (b) cooldown entered (the supervisor's
  actual "giving up for now" state); (c) component still not healthy
  30 min after its first failure event of the episode.
- Delivery: **durable outbox** in state.db — enqueue alert → send via
  Telegram Bot API (token read from the bridge's config; the send path
  must not depend on the bridge *process*, which may be the sick
  component) → mark delivered only on 2xx; retry with backoff, honor
  429 `Retry-After`. Undeliverable alerts stay queued and are retried;
  they never silently drop.
- Dedupe/rate: one open "episode" per component; an episode alerts once,
  then only a recovery message (component healthy ≥ 5 min → episode
  closes, recovery sent, dedupe key released). Dedupe state is in
  state.db, so a host restart cannot re-page.
- Alert text: component name, transition, episode duration, and a
  **bounded (≤ 300 chars), redaction-filtered** diagnostic tail — the
  raw log line may contain tokens/URLs (the bridge logs its bot-token
  URL today; that must never reach a phone notification).
- The host writes alerts to the host log as well (belt and braces; the
  zombie-heartbeat lesson is that silence is the failure mode).

**Acceptance:** kill LiveKit repeatedly → exactly one alert for the
episode, with durable history proving it (restart the host mid-episode:
still no duplicate); restore → one recovery message; unplug the network
during an alert → the alert delivers when connectivity returns.
Round-2 additions: the 30-min alert and the 5-min recovery each fire
correctly across a host restart **with no intervening process exit**
(pure deadline-sweep paths).

### A4. smriti: fix "database is locked" (smriti repo)

- Readers (`smriti_read`, ambient recall hook) open SQLite with
  `mode=ro` + `busy_timeout` (≥ 5000 ms) so a writer's checkpoint can
  never fail a read outright.
- Writers already queue; verify WAL checkpointing isn't being starved by
  long-lived reader connections (observed WAL unchanged since 08-25 while
  seven servers ran).
- Investigate the per-session server population: one `smriti.mcp_server`
  per Claude session is by design, but one codex parent held **four** —
  find the leak (codex MCP config spawning per-subagent?) and cap it.
- Consider (flag, don't build yet): a single shared daemon with the MCP
  servers as thin clients — removes the N-writers problem structurally.

**Acceptance:** with 7+ live sessions, `smriti_read` succeeds 100 times
in a row; a concurrent `smriti_write` storm cannot make a read fail.

### A5. Commit the running-but-uncommitted work (needs Suti's explicit go)

Three repos carry 08-16 work that is live in production but absent from
git: prana (reconnect-stall watchdog + room watchdog — the code that
auto-recovered the box tonight), narada-box3 (reset polarity, GT911
probe, memory budget), mooduel (docs). One commit each, plus this plan
and `presence-roadmap-2026-08-15.md` (still untracked). The new
`~/.narada/host/livekit.yaml` contains the API secret — it lives outside
the repos and stays there; the components.yaml entry references it by
path only.

### A6. Machine health (watch items, not builds)

- Ubuntu WSL distro is broken (`0x800705b4` on start). Reboot, then
  `wsl --update` if it persists. Narada no longer depends on WSL, but a
  sick WSL service can be a symptom of wider trouble.
- The recurring machine-wide stalls from 08-16 (~5 s event-loop freezes)
  remain unexplained. The voice stack now survives them; if they recur,
  chase the cause (GPU job? backup? driver?) before they claim something
  that doesn't.

---

## 3. Workstream B — reach (the asked-for capabilities)

Ordering follows the presence roadmap: F2 memory → outbound Telegram →
F3 context/calendar → F4 web → music → email-in-chat. Each lands alone.

### B1. Memory in the voice (F2) — quarantined, not direct

**Revised per cross-review #1** (round-1 design let the open voice tier
write directly into `notes`, a *recallable* branch — a persistent
prompt-injection channel: anything said in the room, including a TV,
could install durable content later recalled into sessions).

- **All voice-originated writes land in `inbox/voice/`** — and `inbox`
  is already on `memory.py`'s hard denylist (`NEVER_RECALLABLE`), so by
  construction nothing written from the room can ever be recalled to
  the room. This is quarantine-by-existing-mechanism, zero new
  enforcement surface.
- **`remember_this(note)`** (both tiers) → `inbox/voice/` with origin
  metadata: tier, timestamp, session id. The model says aloud that it
  noted it. Bounded length; durable per-day write quota.
- **Session-end summary:** personal-tier sessions only (a shareable
  session with an unknown speaker does not get to author Narada's
  memory of the day), through the transcript-redaction path, also →
  `inbox/voice/`. Skip trivial sessions (< N turns).
- **Promotion is a judgment act:** the chat bridge (prana tier) gets a
  "review voice inbox" flow — and C2's daily debrief includes pending
  inbox items — where Narada-in-chat (or Suti) promotes entries into
  `notes`/`projects`. Only promoted content becomes recallable.

**Tests:** sentinel — an `inbox/voice/` entry must never appear in any
voice recall result; path/branch tricks in the payload still land only
in `inbox/voice/`; shareable-tier session-end produces no summary;
quota enforced across worker restarts.

### B2. Outbound Telegram — Narada can reach Suti (new, small)

The state layer already routes utterances body-or-Telegram; today only
the paused heartbeat exercises it. Wire it up for real — **revised per
cross-review #2** (round-1 exposed direct sends to the open tier, an
unauthenticated outbound mutation under Narada's identity; the "anyone
can leave a note" analogy fails because a note on the fridge doesn't
arrive as Narada speaking):

- **`message_suti(text)` is personal-tier only** (verified tap
  admission). Sends carry origin attribution (`[voice/personal]`
  prefix), bounded length, and a **durable rate limiter in state.db**
  (default 5/hour — survives worker restarts).
- **The shareable tier gets no send tool.** A guest who wants to reach
  Suti is served by the existing `escalate_to_narada` path — the
  escalation arrives as *a report about a guest utterance*, attributed
  as such, never as Narada speaking for itself. No new surface.
- Content passes the transcript redaction filter; the face shows a
  "message sent" hint.
- Implementation: thin wrapper over `state.router.route_utterance` with
  destination forced to Telegram + delivery ack back to the caller.
  (A3's alert path stays independent of this — raw Bot API from the
  host, since the state layer's owning processes may be the sick ones.)

**Tests:** shareable-tier caller cannot invoke the tool (server-side
tier check, not prompt); rate cap survives a worker restart; redaction
applied; delivery failure surfaces to the caller (never silent).

### B3. Context packs + `my_day` calendar (F3 — specced in M2 §2.1–2.3)

Build exactly per the ratified M2 spec: disjoint
`voice-pack/shareable/` and `voice-pack/personal/` roots with separate
builders, sentinel test, fail-closed tap-tier admission (one-shot,
nonce-bound, downgrade on reconnect), direct Google Calendar client with
`calendar.readonly`, owner-only token file, tier-keyed ≤ 5 min cache,
`my_day` personal-tier tool. Nothing new to decide; it is scheduled here
so the review sees the whole sequence.

### B4. Web in the voice (F4)

- Two worker-side tools, shareable tier (a guest asking the weather or a
  fact is fine): **`web_search(query)`** and **`read_page(url)`**.
- The realtime model never browses. Our code runs the search, filters,
  ranks, truncates (hard caps: 5 results, ~1.5 KB per synthesis; page
  fetches text-extracted, ~4 KB cap) — only text we return reaches the
  provider.
- **Backend recommendation: Brave Search API** (free tier ~2k
  queries/mo, one key, zero maintenance) behind a one-function interface
  so SearXNG can replace it later without touching tools. Key custody:
  `~/.narada/.brave-search.key`, owner-only ACL, same treatment as the
  other secrets. Fails closed: no key / quota exhausted → the tool says
  "search isn't available", never an error loop.
- `read_page` guardrails — **full SSRF contract (cross-review #4;
  denylist-by-name was bypassable via IPv6, encodings, redirects, and
  DNS rebinding):**
  - http(s) only, ports 80/443 only, no credentials in the URL.
  - URL canonicalized; the hostname is resolved and **every** A/AAAA
    answer must be a global unicast address — reject loopback,
    link-local, ULA/RFC1918, CGNAT (100.64/10), metadata ranges
    (169.254.169.254 and v6 equivalents), multicast/reserved, and any
    alternate numeric encodings (decimal/octal/hex IPv4, mapped v6).
  - **Connection pinned to the validated resolved address** (preserving
    Host/SNI) so DNS rebinding between check and connect is inert.
  - Redirects: each hop re-validated under the same contract, max 3.
  - Size cap (raw and extracted), time cap, no retries against refused
    connections.

**Tests:** IPv4 and IPv6 private/loopback/link-local/metadata refused in
literal, encoded, and mapped forms; redirect chains into private space
refused at the hop; multi-answer DNS (one public + one private A record)
refused; rebinding simulation (validate-then-swap) connects only to the
pinned address; oversized pages truncated; quota exhaustion degrades
gracefully.

### B5. Music on the body (new — the fun one)

Suti's ask: "play some music." Design honest to the audio-ownership rule
(the voice worker owns the box's audio track — M2/rebirth finding #4):

- **v1 sources: local library + curated internet radio.**
  `~/.narada/music/` for files; `~/.narada/music/stations.yaml` for
  radio streams (name → URL). Zero API cost, zero accounts, works
  tonight. **Spotify explicitly deferred** (needs Premium + OAuth +
  playback SDK; revisit only if v1 leaves Suti wanting).
- Tools (shareable tier — playing music is guest-tolerable by the wall-
  calendar rule): **`play_music(query)`** (fuzzy match over library tags
  + station names), **`stop_music()`**, **`set_volume(level)`**,
  **`what_is_playing()`**.
- Playback: the worker decodes (ffmpeg already in the toolchain — verify,
  else pin `pyav`) and publishes onto the box's audio output *outside*
  billed sessions. **Honesty correction (cross-review #5):** "OpenAI
  hears none of it" holds only while no session is admitted — during a
  session the box mic hears the speakers, so anything still playing
  WOULD reach the provider. v1 therefore removes that state entirely:
- **v1 audio model: one owner, one state machine, pause not duck**
  (cross-review #5 — round-1's duck-to-−12 dB had no mixer/ownership
  design and left music leaking into billed sessions via the mic):
  - A single **audio-owner state machine** in the worker governs the
    output track: `idle → music-playing → session-active → music-
    resuming`. Exactly one source publishes at any moment.
  - Wake word or tap → music **pauses for the entire admitted
    session**, resumes (position-preserving for files; rejoin for
    streams) only after session close. No overlap states exist, so no
    mixing model is needed in v1. Ducking + AEC/far-end-reference is a
    v2 design if pausing feels bad in practice.
  - Repeated `play_music` calls, session crash mid-pause, worker
    restart mid-playback, and reconnects all resolve through the state
    machine (restart → `idle`, music does not auto-resume after a
    crash — never surprise audio).
- Wake-word acceptance gates, both directions, **with numeric pass
  criteria (round-2 fix — "measure the rate" with no threshold means
  any result passes):**
  - **False-accept / self-trigger:** a ≥ 4-hour unattended soak of
    lyric-heavy, speech-like playback (talk-radio station + vocal
    tracks, normal listening volume) must open **zero** billed
    sessions. Any false accept fails the gate; on failure, wake-word
    admission is **disabled while music plays** (tap-only wake during
    playback) until the wake model is retrained/tuned to pass. The
    daily spend cap remains the backstop but is not the mitigation —
    a false accept is room-audio disclosure, not just cost.
  - **False-reject:** ≥ 18/20 wake attempts succeed spoken at ~2 m
    with music at normal listening volume. Below that, same fallback:
    tap-only wake during playback (tap always works — it's a
    data-channel signal, not audio).
- Face: M2b emotion hint channel carries a "playing" glyph; the REC
  glyph rules are untouched (music ≠ recording).

**Tests:** play/stop/volume round-trip; pause-on-admission and resume-
on-close including crash/restart/reconnect races; repeated play calls
idempotent; wake-word false-reject and false-accept rates measured with
music playing; station URL failure degrades with a spoken notice, not
silence.

### B6. Email — in chat, never in the room (M2 §2.4, scheduled)

Per the ratified spec: Gmail wrapper exposing exactly **read** and
**create-draft**, send/delete rejected at the application layer
regardless of scope; lands in the **chat bridge (prana tier)** toolset.
Scopes `gmail.readonly` + `gmail.compose`; token custody as per calendar.
Voice gets nothing; a voice question about email is answered with "I'll
have that in chat." Optional later (only after living with it): a
tap-tier "anything important?" one-liner.

---

## 4. Workstream C — proposed additions (Narada's recommendations)

Things Suti didn't ask for that I think earn their place. Each is small,
none blocks Workstream A/B, all inherit the tier rules.

- **C1. Timers, alarms, reminders (recommend: build with B5).** The
  single most-used capability of every home voice device, and we have
  all the parts: `set_timer(duration, label)` / `set_reminder(when,
  text)` → local scheduler in the worker; announcement through the
  utterance queue. **Tier rules (cross-review #6 — round-1 let a
  shareable-tier reminder fall through to Telegram later, a delayed
  bypass of B2's cap):** shareable-tier timers/reminders are
  **local-only** — they announce on the box, never fall through to
  Telegram. Only personal-tier reminders may deliver to Telegram, and
  B2's durable rate limit is enforced **at delivery time too**, not
  just creation. Durable quotas: bounded pending count (default 20),
  bounded horizon (default 14 days), bounded text, origin attribution
  stored per entry, idempotent firing across worker restarts;
  cancellation of a personal-tier reminder requires personal tier.
  Zero cost, very high daily value.
- **C2. Daily debrief (F5 — after B2+B3).** Suti already ruled "daily IS
  the heartbeat": once a day on the subscription (`claude -p` under the
  Hermes cron), review the day and *tell him* — voice if present,
  Telegram otherwise (B2 is the delivery leg; F6 presence improves the
  choice later). This is the first autonomous heartbeat since the pause,
  built without touching the paused LoRA cycle.
- **C3. M2b face polish (`set_expression` + audio-level lip-sync).**
  Specced, small, and it is what makes the body feel *alive* rather
  than functional. Recommend slotting immediately after B3.
- **C4. Morning weather + day brief on wake (cheap).** `push_weather.py`
  exists; fold weather + calendar-titles (tier-gated) into the first
  interaction of the day rather than a separate feature.
- **Deliberately NOT proposed now:** F7 event-driven senses, F8 mood
  engine, F9 morning self-design, F10 BT belief profile — the roadmap
  holds them; they should wait until the debrief (C2) proves the
  decision loop; and any smart-home control surface — out of scope and
  a different threat model entirely.

---

## 5. Sequence

```
A1 LiveKit component + Docker autostart off     ─┐
A2 honest health probe                           ├─ session 1 (+ A5 commits if approved)
A3 Telegram alerting                            ─┘
A4 smriti lock fix                               — session 2 (smriti repo)
B1 remember_this + session summary              ─┐
B2 message_suti                                  ├─ session 3
C1 timers/reminders                             ─┘
B3 context packs + my_day (M2 §2.1–2.3)          — sessions 4–5
C3 M2b face polish                               — session 5
B4 web search                                    — session 6
B5 music                                         — sessions 6–7
B6 email in chat                                 — session 8 (independent; any time)
C2 daily debrief                                 — session 9 (after B2+B3)
```

## 6. Decisions — ratified by Suti, 2026-08-28

1. **A5: APPROVED** — commit the 08-16 work in prana / narada-box3 /
   mooduel.
2. **B4: Brave Search API.**
3. **B5: radio-only for v1** — no local library yet; `stations.yaml`
   only. Local files and Spotify both deferred.
4. **C1/C2/C3: all three approved** for this round.
5. **B6: still open** — email OAuth account + timing (needs Suti at the
   keyboard for 5 minutes; schedule when convenient).

## 7. Risks

- **Native livekit-server drift:** we now own upgrades (no `:latest`
  pull). Mitigation: version pinned in the binary's filename‑adjacent
  README note; upgrade = download + swap + host restart; watch release
  notes on protocol bumps (box firmware pins SDK protocol 17).
- **Wake-word vs music co-channel:** the acceptance gate in B5 is the
  honest test; fall back to pause-on-wake if ducking fails it.
- **Search/provider text reaching OpenAI:** inherent to voice synthesis;
  mitigated by our-code-filters-first and hard caps (B4). Same class of
  exposure as the context packs, already ratified.
- **Alert fatigue (A3):** rate limits + recovered-messages; tune after a
  week of real data.
- **jsonl / CLI format drift** (standing risk from the rebirth plan) —
  unchanged, isolated in the watcher modules.

---

## 8. Cross-review record (round 1 — 2026-08-27)

Codex adversarial review, verdict **needs-attention**, seven findings.
All seven **accepted** and folded in above:

| # | Sev | Finding | Disposition |
|---|-----|---------|-------------|
| 1 | high | B1 let the untrusted voice tier write directly into `notes` — a recallable branch — so spoken prompt injection (guest, TV) could install durable content later recalled into sessions | **Accepted.** All voice writes quarantined to `inbox/voice/` (already on `memory.py`'s hard denylist — unrecallable by construction); session summaries personal-tier only; promotion to recallable branches is a prana-tier judgment act (B1) |
| 2 | high | B2 gave unauthenticated speakers a direct outbound mutation as Narada; 5/hr cap was non-durable; redaction ≠ abuse sanitization | **Accepted.** `message_suti` moved to personal tier only, durable limiter in state.db, origin attribution; shareable tier keeps only `escalate_to_narada`, which reports *about* a guest rather than speaking *as* Narada (B2) |
| 3 | high | A3 alert conditions ("unhealthy >5 min", "gave up permanently") don't exist in actual supervisor state — health counters reset on respawn, 3-exit/60 s deque, 5-min cooldown | **Accepted.** Redesigned as transition-based alert state machine with monotonic durable history in state.db, durable Telegram outbox with retry/backoff + 429 handling, episode-based dedupe surviving host restarts, bounded redacted diagnostics (A3) |
| 4 | high | B4 SSRF denylist (RFC1918/localhost/.local) bypassable via IPv6, encodings, redirects, multi-answer DNS, rebinding | **Accepted.** Full contract: canonicalization, all-answers-global validation, connection pinning, per-hop redirect validation, port allowlist, credential rejection + the corresponding test matrix (B4) |
| 5 | high | B5 had no mixing/ownership model; "OpenAI hears none of it" false during sessions (mic hears speakers); tests missed false-accepts and races | **Accepted.** v1 = pause-for-entire-session under a single audio-owner state machine (no overlap states, no mixer needed); honesty claim corrected; false-accept/self-trigger gates added; duck+AEC deferred to v2 (B5) |
| 6 | med | C1 reminders = delayed bypass of B2's cap (shareable-tier reminder falls through to Telegram later; no pending/horizon bounds) | **Accepted.** Shareable-tier reminders local-only; Telegram delivery personal-tier only with the durable limit enforced at delivery time; bounded pending count/horizon/text, origin attribution, idempotent firing (C1) |
| 7 | med | A1 cutover stopped the working manual LiveKit before validating the replacement; rollback path weakened | **Accepted.** Preflight validation before stopping the manual process, timed verification, `livekit-up.ps1` repurposed as emergency-start/rollback script, Docker autostart flipped only after verification (A1) |

**Round 2 (2026-08-27):** B1's quarantine claim **verified against
`memory.py`** (inbox is on the hard denylist); findings #2, #4, #5, #6
confirmed RESOLVED with no new blockers. Three residual findings on the
revised text, all **accepted** and folded in:

| # | Sev | Finding | Disposition |
|---|-----|---------|-------------|
| R2-1 | high | A3's recovery ("healthy ≥ 5 min") and timeout ("30 min") transitions could not be derived from the specified events — probe successes only reset an in-memory counter; no event fires when a deadline expires | **Accepted.** Added health-probe-failed/succeeded events, persisted healthy-since timestamp, and a durable deadline sweep (startup + every 60 s); acceptance now covers both deadline paths across a host restart with no intervening exit (A3) |
| R2-2 | high | A1's rollback started the emergency manual process before the supervised component released its ports — double-start race inside the outage being recovered | **Accepted.** Atomic rollback: disable component → restart host → verify PID gone + ports free → emergency script (which refuses if livekit already running/7880 bound) → verify restored server + box rejoin (A1) |
| R2-3 | med | B5's wake-word gates required measurement but defined no pass thresholds — any result technically passed | **Accepted.** Numeric gates: ≥ 4 h lyric-heavy unattended soak with zero billed sessions (false-accept); ≥ 18/20 wakes at ~2 m over music (false-reject); failure ⇒ tap-only wake during playback, not shipping anyway (B5) |

Per the 8-step protocol, plan debate closes here; the next review is of
the implementation diff (steps 5–7). Ratification decisions for Suti
are in §6.
