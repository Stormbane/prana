# The personal-agent platform — topology & architecture

**Date:** 2026-09-04
**Status:** DRAFT — Suti's decisions folded in; **awaiting cross-review**
before the code moves. Not canon yet.
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
server (or retires). The box's voice worker can route through it too.

## Layer 2 — transport (Tailscale)

Tailscale is the private mesh that lets the phone reach the home brain
server securely, no public exposure. **It is the same tailnet Suti is
already building for akhada — do not build it twice.**

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

## Open questions for Suti

1. **PWA name** — mukha / dwara / rupa / other? (rules the folder + package)
2. **Product-gating confirmed?** — this spec treats akhada-as-product as
   the post-D5 horizon, architecture-ready but not built. Correct?
3. **Brain server + cost** — default chat model tier (Sonnet vs Haiku)?
   Keep the `claude -p` subscription path as a selectable cheap backend?
4. **Cross-review before code?** (Recommended — this amends ADR-092's
   plan-of-record and defines cross-repo seams.)
