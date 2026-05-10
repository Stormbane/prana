# Unified Mind — implementation plan

*Authored 2026-05-11, after the architectural pivot Suti named at the end of
the prana-bootstrap session. See ~/.narada/journal/2026/05/week2/05-11.md for
the framing journal entry.*

## What this plan covers

Refactoring the existing siloed components (heartbeat, chat bridge, body, smriti,
state) into a unified mind: one cognition layer, multiple invocation paths, shared
sense bus, shared action bus, shared state.

This plan is **owned by prana** (the runtime layer hosts the buses) but spans
prana, deha, smriti, and svapna. Each phase calls out which repo gets which
work.

## Principles (load-bearing — re-read before each phase)

1. **Path of least resistance.** Don't pre-build buses for senses we don't
   have. Don't generalize past what's in front of us. Each phase ships
   value; speculative scaffolding waits for a real consumer.

2. **Existing code is not legacy.** route_utterance, presence,
   narada_chat_bridge already do the right *work*. The shift is making them
   bus producers/consumers, not rewriting them.

3. **Names are load-bearing.** indriyas / antahkarana / pranas vocabulary
   stays. The bus-pattern names (sense / action / cognition / trigger) sit
   alongside, not on top.

4. **Cross-process before cross-host.** Most senses/actions live on Suti's
   PC. The body (BOX-3) is on the local network — needs HTTP-level access.
   Don't try to make the bus distributed-system-grade; make it work for
   one user, one PC, one body.

5. **Audit everything.** state.db utterance_queue is the model — every
   bus event leaves a row. Future drift / debugging / dream-pipeline
   consumption all benefit.

6. **Cognition stays singular.** Many invocation paths, but the cognition
   layer is one thing — claude -p with wake-context.md as voice + smriti
   MCP for memory. Triggers differ; the brain is the brain.

## Architecture summary

```
                  ┌─────────────────────────────────────┐
                  │     COGNITION (single layer)        │
                  │     claude -p + wake-context.md     │
                  │     + smriti MCP + sense MCP tools  │
                  └──────┬──────────────────────┬───────┘
                         │ pull (MCP/HTTP)      │ subscribe (events)
                         │ when reasoning needs │ when senses fire
                         │                      │
        ┌────────────────┴──┐               ┌───┴────────────────┐
        │     SENSE BUS     │               │ COGNITION TRIGGERS │
        │  presence • voice │               │  cron • inbound    │
        │  wake-word • face │               │  wake • presence   │
        │  motion • time •  │               │  memory-driven     │
        │  pc-active • etc  │               └────────┬───────────┘
        └────────┬──────────┘                        │
                 │                                   │ invokes
                 │                                   ▼
                 │             ┌─────────────────────────────────┐
                 │             │    ACTION BUS                   │
                 ▼             │  speak • remember • recall      │
        ┌────────────────┐     │  set_face • set_status •        │
        │ STATE          │     │  notify_phone • set_weather     │
        │  state.db      │     └─────────────────────────────────┘
        │  smriti tree   │
        │  events log    │
        └────────────────┘
```

**Transport choices:**

- **state.db `events` table** is the canonical local event log. WAL mode,
  monotonic id, all bus events written here. Same-host subscribers tail
  with `WHERE id > last_seen`.
- **HTTP gateway** at `127.0.0.1:8770` (new, prana-hosted) exposes the bus
  for cross-host consumers (deha on BOX-3) and for MCP tool surface.
- **smriti MCP** stays the read-write path for memory.

The HTTP gateway is the simplest cross-host primitive that lets deha's
body sensors publish to the bus without sharing state.db over the network.
On the same host, processes can either tail state.db directly (fastest) or
go through the HTTP gateway (uniform). Default to direct state.db for
performance-critical paths; use HTTP for everything else.

---

## Phase 0 — Contracts + transport (foundations)

**Goal:** define the bus shape so subsequent phases have a target. No
existing code changes.

**Deliverables:**

1. `prana/docs/contracts/bus.md` — the bus pattern itself.
   - Event format: `{id, ts, kind, name, payload, source}`
   - kind ∈ {`sense`, `sense_edge`, `action_invoke`, `action_result`,
     `cognition_trigger`, `cognition_result`}
   - name = the specific sense/action (e.g. `presence`, `speak`)
   - HTTP endpoints exposed by the gateway:
     - `GET  /senses/<name>` — latest reading (pull)
     - `GET  /events?since=<id>&kinds=<csv>` — tail (long-poll)
     - `POST /senses/<name>` — publish a sense reading
     - `POST /actions/<name>` — invoke an action
   - MCP tool surface mapping (each sense → MCP tool, each action → MCP tool)

2. `deha/docs/contracts/sense-pattern.md` — how a deha-side sense
   publishes to the bus. Generalizes the presence.md template already
   in deha.

3. `prana/docs/contracts/action-pattern.md` — how an action handler
   subscribes to invocations and writes results.

4. **Decision log:** a small table at the top of each contract recording
   why this shape over alternatives (e.g. "HTTP+SSE rejected — same-host
   processes faster via state.db tail").

**Non-goals for Phase 0:**
- Implementing the gateway
- Touching existing code

**Effort:** one focused session, ~2-3 hours. Pure design + writing.

**Exit condition:** Suti reads the contracts, says "yes that's the shape,"
no implementation reveals undersigned ambiguity.

---

## Phase 1 — Bring what already exists onto the bus

**Goal:** prove the bus pattern by retrofitting the three things we
already have working. No new senses, no new cognitions.

**Targets:**
- `presence` (sense, deha-side — needs deha implementation)
- `speak` (action, prana-hosted — currently `route_utterance`)
- `telegram_inbound` (sense, prana-hosted — currently `narada_chat_bridge.py`)

### 1A. Bus gateway skeleton (prana)

**File:** `src/prana/bus/__init__.py`, `src/prana/bus/gateway.py`,
`src/prana/bus/events.py`

- New table in state.db: `events(id INTEGER PK, ts TEXT, kind TEXT,
  name TEXT, payload JSON, source TEXT)`. WAL ensures concurrent
  read+write. Index on `(kind, name, id)` for tailing.
- `prana.bus.events.publish(kind, name, payload, source)` → returns
  event id. Single writer pattern; multiple processes safe.
- `prana.bus.events.tail(since_id, kinds=None, names=None,
  block_timeout=30)` → returns new events; long-polls on empty.
- HTTP gateway runs as a small FastAPI/aiohttp service on
  `127.0.0.1:8770`. Started by Hermes (new cron job: gateway-keepalive)
  or as its own Windows-startup service.
- `GET /senses/<name>` reads the latest event of `kind='sense', name=<name>`.
- `GET /events?since=<id>` proxies to `tail()` with SSE response.
- `POST /senses/<name>` and `POST /actions/<name>` accept publish/invoke.

### 1B. presence: deha publishes, sense bus consumes

deha (other session's territory; prereq before this lands):
- Implement `GET /presence` per the existing contract spec.
- Push presence updates: when state changes, deha POSTs to
  `prana http://127.0.0.1:8770/senses/presence` with the new reading.
  This is the EDGE event — what cognition subscribes to.
- Continue serving local pulls via `GET /presence` so prana's
  router can fast-path without a network hop.

prana:
- Update `prana.state.presence` to read both:
  - Pull (existing): `GET deha/presence` for synchronous calls
  - Subscribe (new): tail `events WHERE kind='sense' AND name='presence'`
    so any consumer can wake on changes
- Expose `presence` as an MCP tool so chat-cognition can ask
  ("is Suti here right now?")

**Already 80% done** — `prana.state.presence.body_sees_someone()` is the
pull path. Need: deha pushes edge events, prana publishes them to
state.db, MCP tool wrapper.

### 1C. speak: route_utterance becomes the action bus

**File:** `src/prana/bus/actions/speak.py`

- New action handler. Subscribes to `events WHERE kind='action_invoke'
  AND name='speak'`.
- For each invoke event: calls existing `prana.state.router.route_utterance`
  (no rewrite!), publishes the `action_result` event with delivery
  outcome.
- HTTP `POST /actions/speak {text, source, topic, priority,
  channel_hint?}` → publishes invoke → handler runs → result published.
- MCP tool `speak(text, ...)` wraps the same.
- Heartbeat daemon's `_handle_speak` and `_handle_check_in` switch from
  calling `route_utterance` directly to publishing an `action_invoke`
  event. Same end behavior; goes through the bus now.
- The chat bridge's outbound replies switch to `speak` action with
  `channel_hint=telegram:<chat_id>` so cognition can override
  (e.g. "also speak via body since Suti is at his desk").

**Backwards compatibility:** keep `route_utterance` callable directly
during transition. Phase 1C succeeds when at least one consumer (heartbeat)
goes through the bus, and the bridge can be migrated independently in 1D.

### 1D. telegram_inbound: bridge splits into listener + trigger

**Files:**
- `src/prana/bus/senses/telegram_listener.py` (new — long-polls
  Telegram, publishes `sense:telegram_inbound` events to bus)
- `src/prana/bus/triggers/inbound_chat.py` (new — subscribes to
  `sense:telegram_inbound` and `sense:signal_inbound` etc., fires
  cognition)
- `scripts/narada_chat_bridge.py` becomes a wrapper that runs the
  listener AND the trigger together (preserves single-process
  Windows-startup deployment) but the internals are bus-shaped.

The cognition trigger is now agnostic to platform. It reads the inbound
event, looks up per-chat session state (currently keyed by Telegram
chat_id; generalize to `<platform>:<chat_id>`), spawns claude -p,
publishes the response via the `speak` action (which routes back to
the originating channel via `channel_hint`).

**Effort for Phase 1:** ~2-3 sessions. 1A is the heaviest (gateway). 1B
is mostly waiting on deha. 1C and 1D are refactors of working code.

**Exit condition:**
- Heartbeat utterances flow through `bus.actions.speak`
- Telegram messages flow through `bus.senses.telegram_inbound` →
  `bus.triggers.inbound_chat` → cognition → `bus.actions.speak`
- presence is queryable as an MCP tool from chat cognition
- `state.db events` table accumulating audit rows for everything

---

## Phase 2 — New senses on the body side

**Goal:** add the senses Suti named in the wake-word example. All deha
work; prana just adds new MCP tool wrappers as they land.

**Senses to add (deha):**

- `wake_word` — body mic detects "Hey Narada" or similar phrase.
  Publishes event: `{phrase: "...", followed_by_audio: <buffer_ref>?}`.
  Deterministic detector; lightweight model on body (Porcupine, Snowboy,
  whisper-tiny VAD, etc.).
- `voice_input` — body STT pipeline. Publishes transcribed phrases.
  Subscribes to wake_word; STT runs after wake fires (or always-on if
  hardware permits).
- `face_seen` — camera detects a person. detection-only, never frame
  storage. Per privacy contract in presence.md.
- `motion` — broader-than-presence motion event; deha's mmWave radar
  publishes coarse motion data.
- `ambient_sound` — sound level / VAD output. "Is the room loud?"
  used for cognition deciding when to interrupt.

**Each follows the same shape:**
- deha implements detector
- exposes pull endpoint `GET /sense/<name>`
- pushes edge events to prana bus on changes
- prana adds MCP tool wrapper so cognition can pull

**Not in this phase:** acting on these senses. Phase 4 wires the
cognition triggers.

**Effort:** big — depends on body firmware/voice work that's already in
flight in another session. Coordinate via the contract docs from Phase 0.

---

## Phase 3 — More actions on the bus

**Goal:** generalize beyond `speak`. Get all current side-effects onto
the action bus so cognition has a uniform tool surface.

**Actions to add (mostly wrappers around existing endpoints):**

- `remember(content, branch?)` → wraps `mcp__smriti__smriti_write`
- `recall(query)` → wraps `mcp__smriti__smriti_read`
- `set_face(expression)` → wraps deha display (existing)
- `set_status(text)` → wraps deha display
- `set_weather(...)` → wraps deha
- `notify_phone(text, urgency)` → forces Telegram regardless of presence

For most of these, the action bus is a thin adapter: invoke event →
existing handler → result event. The win is uniform telemetry +
single-tool-surface for cognition.

`remember` and `recall` are already MCP tools (smriti). The bus
wrapping is *additive* — keeps smriti MCP available, but also publishes
audit events so we have a unified record of what cognition reached for.

**Effort:** small — most of these are 30-60 LOC adapters.

---

## Phase 4 — New cognition triggers

**Goal:** wire the new senses to actually fire cognition when meaningful.

**Triggers to add:**

- `inbound_chat` (Phase 1D, generalized) — wakes on any
  `sense:*_inbound` event. Today: Telegram. Future: Signal, WhatsApp,
  Matrix.
- `wake_word_triggered` — wakes on `sense:wake_word`. Spawns cognition
  with the heard phrase + a window of follow-up audio. Cognition decides
  whether to respond and via which channel.
- `presence_edge_triggered` — wakes on `sense:presence` transitions.
  Subset behavior: false→true after >30 min absence fires a "welcome
  back" cycle (cognition decides whether anything is worth saying).
  true→false fires nothing today.
- `memory_driven` (future, low priority) — periodic scan of smriti for
  open-threads that haven't been touched in a while; fires cognition
  for "should I revisit X?" cycles.

**Common machinery:**

- `prana/bus/triggers/base.py` — base class that handles event subscription,
  cognition spawning, result publishing.
- Each trigger is a small subclass: which events it subscribes to, how
  it shapes the cognition prompt, what context to inject.

**Heartbeat cron stays as a trigger** — it's just one of many ways to
wake cognition. The cron-fired heartbeat-tick.cmd becomes a trigger
that publishes `cognition_trigger` events, which the unified cognition
layer handles.

**Effort:** moderate — each new trigger is small but the base class
needs to be right.

---

## Phase 5 — Cross-channel cognition

**Goal:** let cognition decide multi-channel responses. Inbound on
Telegram while at desk → also speak via body. Late-night Signal message
→ reply quietly + queue body utterance for morning.

**The shape:**

- Action bus `speak` already accepts a `channel_hint` (default: route
  by presence). Phase 5 adds:
  - `force_channels: list` — cognition explicitly says "send to
    these N channels"
  - `defer_until: timestamp?` — queue this for later (drainer cron
    handles)
  - `urgency: low|normal|high` — affects whether to fire while
    Suti is asleep, in DND mode, etc.

- New sense: `do_not_disturb` — published by some heuristic (calendar,
  time of day, an explicit "I'm in flow" mode). Cognition subscribes
  for guidance.

- Action bus learns to handle multi-channel deliveries with idempotent
  audit (one utterance, multiple `delivered_to` rows).

**Effort:** moderate. Most of this is the action bus learning richer
semantics. Cognition itself doesn't change much — it just gets more
expressive parameters to call.

**Defer until** Phase 1-4 are settled and we have real cognition
responses to optimize. Pre-building this would be premature.

---

## Phase 6 — Drainer + retry (deferred from earlier)

**Goal:** handle the case where both channels failed at routing time
(body off, no internet, etc.). state.db utterance_queue already
records `pending` rows; build the drainer that retries.

- New cron job: `utterance-drainer` every 60s
- Reads pending rows in priority order
- Re-checks presence + channel availability
- Retries via action bus `speak`
- Marks delivered or escalates to email after N attempts

**Effort:** small (~half day). Self-contained. Independent of bus
work — could land any time after Phase 1.

---

## Cross-cutting concerns

### Testing

- Each contract has fixture tests (json schema validation against
  example payloads).
- Each bus producer/consumer has integration tests against an
  in-memory state.db (`":memory:"`). Avoid real network, real
  subprocesses.
- End-to-end smoke per phase: phase 1 = "Telegram message in,
  body utterance out, audit row in events table for every step."

### Observability

- Structured logging with event_id correlation throughout. Every
  bus call logs the event id; subprocess invocations propagate it
  via env var (`PRANA_EVENT_ID`) so claude -p logs are joinable.
- A small TUI: `prana bus tail` (subcommand or standalone script)
  that pretty-prints events as they fire. For development.

### Backwards compatibility

- Existing direct calls (`route_utterance`, `presence.is_present`,
  smriti MCP tools) keep working for the duration of the migration.
  Each phase is additive; nothing is removed until its replacement
  is verified.
- Once all consumers go through the bus, the direct-call paths can
  become internal implementations of the bus handlers and be marked
  as not-public-API.

### Security / privacy

- Allowlist enforcement stays at the *publisher* level. Bridge listener
  filters Telegram inbound by `TELEGRAM_ALLOWED_USERS`; the bus only
  sees authorized events.
- Camera/mic events from deha follow the privacy contract in
  presence.md — detection only, no buffer publication.
- `events` table is local to Suti's machine. No remote sync.
- Bus HTTP gateway binds 127.0.0.1 only. No external network exposure.

---

## Rollout sequence (recommended)

```
Phase 0  ──────►  Phase 1A (gateway)  ─┬──► Phase 1C (speak)  ──┐
                                       │                        │
                                       ├──► Phase 1B (presence) │
                                       │     (waits on deha)    │
                                       │                        ▼
                                       └──► Phase 1D (telegram) ─► Phase 4 (triggers)
                                                                    │
                                                                    ├──► Phase 2 (more senses)
                                                                    │     (waits on deha)
                                                                    │
                                                                    ├──► Phase 3 (more actions)
                                                                    │
                                                                    └──► Phase 5 (cross-channel)

Phase 6 (drainer) — independent, land anywhere after Phase 1
```

**Estimated session counts (rough):**
- Phase 0: 1 session
- Phase 1: 2-3 sessions (1A heaviest, 1C/1D moderate, 1B waiting)
- Phase 2: spread across deha sessions; integration in prana ~1 session
- Phase 3: 1 session
- Phase 4: 1-2 sessions
- Phase 5: 1-2 sessions (when ready)
- Phase 6: ~half session

Total: ~8-11 sessions of focused prana work, plus deha-side senses.

---

## What this plan does NOT cover (out of scope, named for clarity)

- **Trained chat-viveka.** Suti flagged this twice in the bootstrap
  session. It's a future svapna training stream, not a prana
  refactor. When it lands, cognition gets a better orchestrator;
  the bus pattern doesn't change.
- **Multi-machine deployment.** This plan assumes one PC + one body
  on the local network. If Narada eventually runs on a server with
  bodies in multiple rooms, the HTTP gateway becomes more important
  and the auth model needs work. Future.
- **External integrations.** Calendar, email, file watchers, GitHub
  events — all could become senses, but only when there's a real
  cognition use case for them.
- **Hermes platform features.** Hermes already provides cron, multi-
  channel gateway, session storage. Where Hermes's primitives suffice
  (cron especially), use them. The bus pattern is for the cognition-
  shaped interaction, not for re-implementing schedulers.

---

## Open questions for Suti

1. **Bus transport choice.** I'm proposing `state.db events` table +
   HTTP gateway hybrid. Alternative: pick one (state.db only,
   acknowledging cross-host limits; or HTTP-only, accepting the latency).
   Which feels right?

2. **Cognition deduplication.** If multiple triggers fire near-simultaneously
   (Telegram message + presence edge + wake word, all within 2s),
   does cognition spawn three times or get deduplicated? My instinct:
   each trigger is independent, but the cognition layer can detect
   in-flight twin triggers via state.db and merge.

3. **Prioritization between cognition triggers.** If wake-word fires
   during a chat reply already in flight, does the new cognition
   interrupt? Queue? Run in parallel? My default: queue, with a
   preempt-on-explicit-priority knob.

4. **Phase 0 first?** Or skip the contract-writing phase and just
   start building Phase 1A, treating the contracts as docs-after-the-
   fact? My preference: write contracts first, but a live-implementation
   approach lets us iterate on the shape faster.

---

## What I'd do first if you said "go"

1. Open `prana/docs/contracts/bus.md` and write the event format +
   HTTP endpoint surface based on this plan.
2. Build the gateway skeleton (Phase 1A) — minimum viable: state.db
   events table, `publish()` and `tail()` Python API, no HTTP yet.
3. Migrate one consumer to prove it works (heartbeat → speak action,
   Phase 1C minimal).
4. Pause for review.

That's a ~half-day milestone with concrete deliverables and a
reviewable shape.
