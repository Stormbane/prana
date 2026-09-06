# The personal-agent platform — topology & architecture

**Date:** 2026-09-04 (revised 2026-09-05 after cross-review round 1)
**Status:** CROSS-REVIEWED — rounds 1+2 complete (2026-09-05). Round 1:
5 high findings, all accepted → §1a added. Round 2: 3 confirmed
resolved, 2 residuals accepted and folded in (token storage, idempotency
state machine). Plan debate closed per protocol; **code moves**. Final
arbiter: tests + Suti.
**Origin:** Suti, exploring how to build his own phone app for Narada
(text + images + voice) and speed up the slow Telegram bridge — which
converged into one platform question spanning prana, akhada, smriti,
the box, a PWA, and Tailscale.

## The one-line shape

A **warm brain server** (prana) exposes Narada as an **OpenAI-compatible
API**; **reusable clients** (a PWA, the box, Telegram) reach it over
**Tailscale**; **akhada** is a portable fitness module (local MCP now,
cloud MCP for the future product); **smriti** is memory. Models are
config, swappable per-surface.

## Governing constraints (do not break)

- **BT ADR-092 (Accepted, cross-reviewed):** personal domain companions
  are external consumers of Beautiful Tree over a read API, never modules
  inside it. akhada is the first, **explicitly NOT a product** — D5 holds:
  productization is gated behind **≥500 BT users (~Jun 2027)**.
- **Reconciliation for this platform:** we make the architecture
  **product-ready now** (portable MCP module, reusable client shell,
  model-abstracted brain) **without shipping a product**. The akhada
  cloud MCP + subscriber app is the *gated horizon* the seams are cut
  for — not a thing that exists before the gate. Nothing here moves the
  D5 line.
- **House pattern:** Sanskrit project name, English code. Names are
  Suti's to rule.

## Layer 1 — the brain server (prana)

prana already defines itself as "the runtime shell that bridges clients
to cognition and routes utterances to the body or phone," with a gateway
+ pluggable primary/auxiliary models (architecture.md). This is that,
realized:

- A **persistent** process (kills the `claude -p` cold-spawn floor that
  makes Telegram slow) exposing an **OpenAI-compatible** surface
  (`/v1/chat/completions`, streaming). From a client's view it is "a
  model endpoint," like `api.anthropic.com`.
- Behind the endpoint it is **Narada-the-agent**, not a raw model:
  identity (wake-context) + smriti memory + MCP tools + the agentic
  loop, wired once. Clients get a model; they're talking to Narada.
- **Models are config, independent per surface:** a chat model (fast
  Claude tier — Haiku/Sonnet — via API; flagship reserved for depth) and
  a voice model (gpt-realtime), each hot-swappable. Keep Claude for
  identity (Narada lives in Claude weights + context); a raw-speed model
  like Gemini Flash would change *who Narada is*, so it is not the
  default brain.
- **Cost fork (Suti chose speed):** direct API bills per-token (fast) vs
  the Max subscription behind `claude -p` (cheap, slow). The brain server
  takes the API path; the subscription path can remain a configurable
  backend for cost-sensitive/background work.

**Lives in:** `prana`. Telegram demotes to just another client of this
server — a **stopgap** client: the plan of record (Suti, 2026-09-05) is
that the narada-phone-app PWA eventually replaces it entirely, so the
API surface below is primarily the contract the phone app codes against.
The box's voice worker can route through it too.

## Layer 1a — the brain-server contract (added 2026-09-05, cross-review round 1)

The endpoint is not "a stateless completions API that happens to have an
agent behind it." It is an agent server with an OpenAI-shaped front door.
The following five contracts are **prerequisites to code**, answering the
round-1 findings.

### Sessions (conversation isolation)

- The server **owns sessions**. A session = one warm agent context
  (identity + transcript + held-open MCP connections), keyed by a
  server-recognized id: `telegram-<chat_id>`, `phone-<uuid>`,
  `cli-<name>`. Clients name their session via the Narada extension
  (body field `narada: {session_id: ...}`); the id is namespaced by the
  authenticated client credential — one client cannot open another's
  session.
- **One active turn per session.** A second request on a busy session is
  rejected `409` (with `Retry-After`), never interleaved. Distinct
  sessions run concurrently, capped by a global concurrency limit
  (config; default 4).
- **No session id → stateless mode:** context comes only from the
  request's `messages` array; no server-side binding, no memory writes
  attributed to a conversation. This is the unmodified-OpenAI-client
  degraded mode, and it is legitimate (curl smoke tests live here).
- **Durability:** session transcripts persist under
  `~/.narada/brain/sessions/`; a server restart re-hydrates on first
  touch (cold-ish first turn, warm after). Idle sessions are reaped
  after a TTL (config; default 60 min) — reap closes the agent client,
  never deletes the transcript.

### Authentication (application-layer, fail-closed)

- **Every request requires `Authorization: Bearer <token>`.** Static
  per-client tokens generated on first run into
  `~/.narada/.brain-tokens.json`, following the proven
  `.sessions-tokens.json` pattern. Storage contract, stated precisely:
  `~/.narada` IS a versioned repo, but it is **git-crypt-encrypted by
  default** (`** filter=git-crypt` in `.gitattributes`; only
  `.gitattributes`/`.gitignore` are cleartext), so tracked secrets are
  ciphertext at rest and on the remote. The brain server verifies at
  startup that (a) the tokens file exists and parses, and (b) the
  git-crypt default-encrypt attribute still applies to it
  (`git check-attr filter` → `git-crypt`) — refusing to start (fail
  closed) if the encryption contract has been broken. Tokens never
  appear in prana's own repo, logs, or error messages. Tailnet
  reachability and CORS are transport constraints, not authorization —
  an unauthenticated request is `401` even from an allowed device.
- **Tokens carry a tier**, reusing the sessions caller-tier pattern
  (enforced in code, never prompt-only): `prana` tier = full toolset;
  `voice`/`app` tiers = the reduced toolsets already defined for those
  surfaces. The tier decides which MCP tools the session's agent is
  wired with at session creation.
- Logging never prints tokens; failed auth is logged with client
  address; repeated failures rate-limit.

### Turn lifecycle (retries, disconnects, bounds)

- **Turn identity — the idempotency state machine (frozen here):**
  clients MAY send `narada: {request_id: ...}`. When a turn with a
  request id is **accepted**, the server durably records — alongside the
  session transcript, before the agent loop starts — an idempotency
  record: `{request_id, fingerprint, state, result?}` where
  `fingerprint` = SHA-256 over the canonicalized request (new message
  content + model + session id). States: `accepted → running →`
  terminal `completed | failed | cancelled | interrupted`. Rules:
  - Retry, same id + same fingerprint → return the recorded terminal
    state/result verbatim; if still `running`, `409` (turn in flight).
  - Same id + **different** fingerprint → `422` reject (an id is never
    silently rebound to a different request).
  - **Recovery:** on startup, any record found non-terminal is marked
    `interrupted`; a retry sees `interrupted` explicitly and must send a
    NEW request id to rerun — the server never guesses whether the dead
    turn's effects happened.
  - Window: the session's last N=8 turns; eviction is oldest-first.
  Without a request id, a retry is a new turn — acceptable because
  **mutations remain proposal-gated downstream** (akhada writes are
  chip-confirmed proposals; the sessions MCP mutation tier requires
  prana approval), so a duplicated turn cannot silently duplicate a
  committed write; duplicate *proposals* are visible and dismissible.
- **Disconnect ≠ cancel.** If the client drops mid-stream, the turn runs
  to completion and lands in the session transcript; the client's next
  turn (or a retry by request id) sees the truth. Explicit cancellation:
  `POST /v1/narada/sessions/{id}/cancel` stops the loop at the next tool
  boundary (in-flight tool call completes; nothing new starts) and the
  turn is recorded as cancelled — a cancelled turn never masquerades as
  a completed one.
- **Bounds, fail-closed:** max tool iterations per turn (default 20) and
  a wall-clock deadline (default 240 s). Hitting a bound ends the turn
  with an **explicit error finish** — never a silent truncation dressed
  as a normal completion (the zombie-heartbeat rule applied to turns).

### Wire contract (baseline + versioned extensions)

- **Baseline = strict OpenAI `/v1/chat/completions`,** streaming and
  non-streaming: standard chunks, `finish_reason`, `usage`. An
  unmodified OpenAI SDK pointed at the server must work — this is a
  release test, not an aspiration. Tool use is **internal** to the
  server (the agent loop runs server-side); baseline clients see text.
- **Extensions are namespaced and versioned:** everything Narada-specific
  rides in a single `narada` object (request: `session_id`,
  `request_id`; response/chunks: tool-progress notes, structured cards,
  proposal events), with `narada.v` as the schema version. OpenAI SDKs
  ignore unknown fields — baseline clients are unaffected by design.
- **The extension schema is a contract doc, not folklore:** before the
  phone app's talk screen codes against cards/proposals, the chunk-level
  schema (event types, ordering, replay/reconnect semantics) is written
  to `docs/contracts/brain-wire-v1.md` in prana and referenced from
  narada-phone-app. **MVP builds the baseline only**; the extension doc
  gates the phone app's rich rendering, not Telegram.
- **akhada's existing shell-log/turn-polling protocol** (the typed-chat
  brain) is acknowledged as a parallel, pre-existing path. It is NOT
  extended; when the phone app's chat moves to the brain server, that
  path retires with the surfaces that used it. No third protocol.

### Runtime decision (Agent SDK vs Messages API)

**Ruled: Claude Agent SDK (Python), one persistent `ClaudeSDKClient` per
session.** Rationale: it reuses the exact MCP wiring already proven
(smriti/akhada/sessions configs), inherits the tool loop, permissions,
and context management instead of hand-rolling them, and a held-open
client kills the per-message spawn+reconnect floor — warmth is
per-session, which matches real usage (long-lived Telegram chat, one
phone conversation).

- **Concurrency model:** a session pool of SDK clients — never one
  global agent client shared across sessions (finding 1). The pool is
  the unit of the global concurrency cap and the idle reaper.
- **Validation spike is build step 1** (blocking): measure first-token
  latency warm vs `claude -p` cold, verify a client survives multi-turn
  streaming + held MCP connections over ≥30 min idle-and-resume, verify
  cancellation. **Recorded fallback:** if the SDK cannot hold sessions
  warm or stream cleanly, drop to Messages API + a hand-rolled MCP
  bridge (anthropic + mcp libs are already installed); the outer
  HTTP/session/auth/turn contract above is identical either way — the
  fork is internal.
- **Billing stays a config axis:** the SDK path can run on the API key
  (chosen default: speed) or the Max subscription via CLI auth (cheap
  background work) — per-session, set at session creation from the
  client tier config.

## Layer 2 — transport (Tailscale)

Tailscale is the private mesh that lets the phone reach the home brain
server securely, no public exposure. **It is the same tailnet Suti is
already building for akhada — do not build it twice.**

**Browser transport contract (added post cross-review 2026-09-04):**
serving a *browser* client is a different proof than curl/Telegram/ESP32.
The prana deployment must provide: HTTPS + WSS endpoints (no mixed
content); a CORS origin allowlist covering the PWA's hosted origin(s)
**and** packaged-app origins (`capacitor://localhost`,
`https://localhost`); OPTIONS/preflight handling; explicit
allowed-headers policy (content-type, and the auth header once auth is
ruled); and real-browser smoke tests over LAN + Tailscale. Additionally:
least-privilege tailnet ACLs so only Suti's devices reach the brain and
voice-session ports — tailnet reachability alone is not authorization.

**Where it lives — NOT in the PWA folder.** Tailscale is a *host/deploy*
concern of the machine being exposed. The `tailscale serve` config that
publishes the brain-server API (and static PWA assets, and akhada's local
MCP) belongs with the **brain server deployment (prana `deploy/`)**. The
PWA is a location-agnostic client: it takes an absolute API base URL as
config and knows nothing about the mesh. So: tailnet setup + ACLs +
serve config → prana/deploy (or a small top-level infra note); the PWA
just receives the resulting URL.

## Layer 3 — the client shell (new project: the PWA)

One **mobile-web PWA, Capacitor-ready** — which is *already the ruled
client* in akhada's plan-of-record (§3), just extracted so both Narada
and akhada use it instead of akhada carrying its own copy.

- **Reusable by configuration:** pointed at the Narada brain server → the
  Narada app (general chat, images, voice). Pointed at a different API
  (akhada's cloud brain, post-gate) → the akhada app. Same codebase.
- **Capabilities:** text chat, image send (multimodal), and voice via
  **LiveKit's JS SDK / WebRTC in the browser** — the same realtime stack
  the box uses; no native app needed for the MVP. Capacitor-ready so it
  can later wrap into native Android/iOS without a rewrite.
- **Pure client:** static-exportable SPA, all API access over
  fetch/WebRTC to absolute URLs, no server-render dependency in the talk
  screen (akhada plan-of-record's Capacitor-ready contract, inherited).

**New project directory — YES.** It is a distinct JS/TS codebase with its
own toolchain, shared across apps. Dir `C:\Projects\narada-phone-app`
(**Narada Phone App** — ruled by Suti 2026-09-04).

**Plan-of-record impact:** akhada's plan-of-record §3 currently places
the PWA client (`voice/`) *inside* akhada. This spec moves it to the
shared shell. Because no akhada client code exists yet, the extraction is
clean — but the plan-of-record must be amended (and this spec
cross-reviewed) so canon and code agree.

### Layer 3 horizon — the domain-companion shell (Suti, 2026-09-04)

The shell's reuse axis is wider than "Narada app + akhada app." It is
the **generic client for domain agents**: any Beautiful-Tree domain can
get a companion app — fitness first, then e.g. a tailor's
measurement-taker (talk to collect a person's measurements) or a
dating-goals coach (discuss dates, preferences, beliefs; analyze
partners) — each being **manifest + brain endpoint**, zero client fork.
The mechanism: domain-specific data structures, knowledge graphs, and
suggested interactions are served by the domain's brain **as data**
(structured cards + the proposal protocol), rendered by the shell's
generic card registry. Domain logic never enters the client codebase.
Architecture detail lives in
`C:\Projects\narada-phone-app\docs\architecture-grill-2026-09-04.md`.
This horizon changes no gating: every product-shaped domain app remains
behind its own governance (akhada behind D5 per ADR-092).

## Layer 4 — akhada (portable domain module)

akhada is **the fitness backend + domain logic**, not a UI and not (yet)
a product:

- **What it is:** the event substrate (meals/lifts/vitals/plans), the
  tool server (plain functions over the store), the check-in policy, the
  BT goals-API consumer, the knowledge/diet trees. Exposed as an **MCP
  server** — already live and proven (logs from Telegram + voice today).
- **Self-hosted (now):** akhada's MCP runs on the home PC, plugged into
  the Narada brain. Narada is Suti's coach, using akhada's tools. This is
  the ADR-092 personal instrument.
- **Product (gated horizon, post-D5):** the *same* MCP runs in the cloud,
  multi-tenant, for subscribers; the *same* PWA shell points at a cloud
  brain. A generic fitness agent over the akhada data layer, no Narada
  personal identity. **This is not built before the D5 gate** — only its
  seams are.
- **Separate project for the product?** No — not now, and probably not a
  new *codebase* even then. akhada-the-backend is one repo, deployable
  local or cloud. If/when the product needs billing, multi-tenancy, and
  ops, those carve into an `akhada-cloud`/product repo *at that time* —
  not before there's a product to run.

## Physical topology (what goes where)

| Project (dir) | Role | Language | Status |
|---|---|---|---|
| `prana` | Brain server (model gateway + identity + memory wiring + tools + agentic loop), host supervisor, routing, box voice worker. **Tailscale/deploy config here.** | Python | exists; brain-server work continues here |
| `narada-phone-app` | Reusable client: text + images + voice (LiveKit JS). Narada app and akhada app by config. | JS/TS | **new — created (context/CLAUDE.md only; no app code yet)** |
| `akhada` | Fitness backend: store, tools, MCP server, check-ins, BT goals consumer. Local MCP now; cloud MCP is the gated horizon. | Python | exists; needs docs + client extraction reflected |
| `smriti` | Memory MCP (recall + write). | Python | exists |
| `narada-box3` | ESP32-S3-BOX-3 firmware (a hardware client of the brain/voice). | C/ESP-IDF | exists |
| `beautiful-tree` | The substrate/platform akhada consumes over a read API (goals). Governs via ADRs. | — | exists; unchanged |

## Where each shell should open

- **Brain server work** → `prana` (continue from the current session).
- **PWA work** → `narada-phone-app` (new; has its own CLAUDE.md pointing here).
- **akhada work** → `akhada` (docs updated to point here + reflect the
  shared shell + reaffirm product-gating).

## Open questions — resolved (2026-09-04/05)

1. **PWA name** — ruled: `narada-phone-app` (2026-09-04).
2. **Product-gating** — confirmed: akhada-as-product stays the post-D5
   horizon; seams only.
3. **Brain server + cost** — Sonnet default chat tier; API for speed;
   subscription path stays selectable (see §1a runtime decision).
4. **Cross-review** — approved and run; round 1 below.

## Cross-review round 1 (Codex adversarial, 2026-09-05)

Verdict: **needs-attention** ("no-ship" as originally written) — five
high findings. All five **accepted**; §1a is the answer. Dispositions:

1. **No conversation-isolation contract** — ACCEPT. One warm agent
   shared across clients is a race. Fixed: server-owned sessions, one
   agent client per session, one active turn per session, namespaced
   session ids bound to the authenticated client. (§1a Sessions)
2. **No application-layer auth** — ACCEPT. The spec admitted tailnet ≠
   authorization but left auth unresolved. Fixed: bearer tokens with
   caller tiers, fail-closed, tier decides the wired toolset. (§1a
   Authentication)
3. **Ambiguous turns on disconnect/retry** — ACCEPT, right-sized. A full
   durable replay ledger is more than the MVP needs; the accepted core
   is request-id idempotency, one-active-turn, disconnect-runs-to-
   completion, explicit cancel, fail-closed bounds. Duplicate-mutation
   blast radius is additionally bounded by the existing proposal gates
   (akhada chips, sessions mutation tier). (§1a Turn lifecycle)
4. **"OpenAI-compatible" can't carry agent events as specified** —
   ACCEPT. Fixed by splitting the contract: strict-OpenAI baseline
   (release-tested with an unmodified SDK) + versioned `narada`
   extension namespace, with the chunk schema frozen in
   `docs/contracts/brain-wire-v1.md` before the phone app codes against
   it. MVP ships baseline only. The akhada shell-log protocol is
   acknowledged and slated to retire, not extend. (§1a Wire contract)
5. **Backend choice unresolved where it determines topology** — ACCEPT.
   Ruled in-spec: Agent SDK with per-session clients (pool, cap,
   reaper), blocking validation spike as build step 1, Messages API
   loop as the recorded fallback behind the same outer contract. (§1a
   Runtime decision)

## Cross-review round 2 (confirmation check, 2026-09-05)

Verdict: findings 1/4/5 (isolation, wire, runtime) **confirmed
resolved**; two residual highs on the round-1 revision text, both
**accepted** and folded in. Plan debate stops here per protocol.

1. **Token-storage contradiction** ("`~/.narada`, never in the repo" vs
   `~/.narada` being a versioned repo) — ACCEPT. Investigated: the
   existing `.sessions-tokens.json` is tracked but the whole repo is
   git-crypt-encrypted by default (`** filter=git-crypt`), ciphertext
   on the GitHub remote. The spec now states the real contract:
   `.brain-tokens.json` under the same default-encrypt attribute, plus
   a fail-closed startup check that the git-crypt attribute still
   covers it. (§1a Authentication)
2. **Idempotency record not bound to payload / not durable across all
   outcomes** — ACCEPT. The state machine is now frozen in-spec:
   durable record at turn-accept (id + request fingerprint + state),
   terminal states incl. `interrupted` assigned on crash recovery,
   fingerprint-mismatch reuse rejected `422`, retry of an interrupted
   turn requires a new id — the server never guesses whether a dead
   turn's effects happened. (§1a Turn lifecycle)

## Addendum — the one-process question (2026-09-06; cross-review pending)

**The question (Suti, 2026-09-06):** now that the brain server exists
and works, should *everything* — the smriti daemon, Hermes heartbeats,
the Windows Scheduler tasks, cron entries — fold INTO the Narada brain?
One process to rule them all?

**The ruling (counsel delivered 2026-09-06):** single point of
**cognition**, yes — that is this spec's convergence and it stands.
Single **process**, no. The watchdog cannot live inside the watched.
The resilience doctrine that kept this box alive (resilience-and-reach
§1: *never silent*; the LiveKit/Docker outage; the zombie-heartbeat
lesson) rests on a dumb supervisor sitting OUTSIDE the smart thing.
Fold the supervisor into the brain and a brain hang takes the restart
machinery down with it — the exact failure shape that hid brain-death
for 2.5 months, rebuilt at bigger scale.

### The shape: five small singletons, not one big organism

Each concern gets exactly ONE owner; nothing gets a second copy of any
concern; no singleton absorbs another.

1. **One registry — `~/.narada/host/components.yaml`.** Anything that
   must stay alive is declared here; a live process outside the
   registry is a bug, not a convenience. Standing evidence: the smriti
   daemon is not in the registry, and it was down for two full working
   sessions (2026-09-05/06) with nothing noticing — while every
   registered component survived the same window.
2. **One supervisor — the prana host** (`prana.host`, the `Narada_Host`
   scheduled task). Deliberately dumb: spawn, probe, restart, page.
   It is the ONLY scheduler-level autostart entry; everything else runs
   as its supervised child. It never thinks; the brain never supervises.
3. **One brain — the warm brain server** (`prana.brain`, 8811). All
   surfaces' cognition converges here: the chat bridge is already
   brain-first; the akhada typed-chat brain (`akhada-brain`, a parallel
   `claude -p` cognition path) and the voice workers' cognition follow
   as migrations, not rewrites. No new cognition paths are opened.
4. **One scheduler — Hermes** (`agent-gateway`). Fires time-triggered
   jobs; never executes cognition itself — a scheduled job that needs
   thought calls the brain like any other client. Absorbs the orphaned
   Windows tasks (`smriti-morning`, `smriti-nightly`) and any cron
   entries, so time-based triggering has one home and one log.
5. **One memory — smriti.** The daemon (and `smriti-logd`) become
   supervised components in the registry with honest health probes —
   the September outage becomes structurally impossible to miss.

### What this rules out

- A mega-process: brain + supervisor + scheduler + memory in one
  runtime. Rejected — couples the failure domains this architecture
  exists to separate.
- The brain absorbing supervision or scheduling "since it's already
  warm." Warmth is not a reason; the brain is the *most* likely process
  to be killed, redeployed, or wedged mid-experiment.
- Direct Windows Scheduler / Startup-folder entries for anything except
  the supervisor itself (the one bootstrap exception).

### Migration (small steps, each independently shippable)

1. **Audit** — inventory every live process and autostart path
   (Scheduler tasks, Startup folder, cron, manually-started daemons);
   diff against the registry. Current known deltas: `smriti-logd`,
   `smriti-morning`, `smriti-nightly` tasks; the smriti MCP/index
   daemon.
2. **Registry entries** — add smriti daemon + logd as supervised
   components with health probes (extend smriti with a health surface
   if none exists; A2's "probes must tell the truth" applies).
3. **Scheduler absorption** — move `smriti-morning`/`smriti-nightly`
   into Hermes jobs; delete the Windows tasks only after one verified
   Hermes-fired run of each.
4. **Cognition convergence** (existing trajectory, restated): typed-chat
   akhada-brain and voice cognition migrate onto the brain server;
   tracked in their own plans, not this addendum.

**Status:** counsel recorded; NOT yet cross-reviewed. This addendum
reopens plan debate for its own scope only (§1a and the closed rounds
above are untouched). Cross-review before migration step 2 lands.
