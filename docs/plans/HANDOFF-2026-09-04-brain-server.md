# HANDOFF — prana brain server (2026-09-04)

You are continuing the **prana brain-server** work in a fresh context.
This is the keystone of the personal-agent platform. Read this, then the
spec, then start.

## The one job

Build the **warm, persistent brain server** that replaces the slow
`claude -p` cold-spawn and becomes the single cognition endpoint for
every Narada surface (phone app, Telegram, box).

- **It exposes an OpenAI-compatible API** (`/v1/chat/completions`,
  streaming). Clients treat it as "a model endpoint."
- **Behind the endpoint it is Narada-the-AGENT**, not a raw model:
  identity (wake-context) + smriti memory + MCP tools (akhada, sessions,
  smriti) + the agentic tool loop, wired once and held warm.
- **Models are config, independent per surface:** chat model (fast
  Claude tier — Sonnet default, Haiku option) and voice model
  (gpt-realtime) set separately. Keep Claude for identity.

## READ FIRST (in order)

1. **The spec** — `docs/plans/personal-agent-platform-2026-09-04.md`.
   The whole topology + the ADR-092 reconciliation. Canonical intent.
2. **prana architecture** — `docs/architecture.md` (already has a
   gateway + pluggable primary/auxiliary model design; this realizes it).
3. **BT ADR-092** — `C:\Projects\beautiful-tree\.ai\decisions\ADR-092-personal-domain-companions-goals-api.md`
   — governance: akhada + any product are D5-gated (≥500 BT users). Build
   seams product-ready, NOT the product.

## Critical design nuance (do not get this wrong)

The speed win is **persistence**, not switching to a paid API. The
`claude -p` slowness is from spawning a NEW process + reconnecting MCP
servers **per message**. A long-lived server that holds ONE agent client
+ open MCP connections kills that floor **regardless** of
subscription-vs-API. So:
- **Warmth** (persistent process, held-open MCP) = the speed fix.
- **API-vs-subscription** = a separate *billing* choice (Suti chose API
  for speed, but keep the subscription path selectable for cheap
  background work).

Open build decision to make early: run the agentic loop via the **Claude
Agent SDK (Python, persistent)** — reuses MCP wiring, can stay warm — vs
the **Messages API + a hand-rolled MCP tool bridge**. The Agent SDK is
the likely fast path; the hard part either way is exposing the agentic
loop as a clean OpenAI-compatible streaming endpoint (the
"agent-as-a-model-endpoint" pattern — the thing the cross-review must
pressure-test).

## What already exists to build on

- **Telegram bridge** — `scripts/narada_chat_bridge.py`. Today: spawns
  `claude -p` per message. This session already sped it up (Sonnet
  model + `--strict-mcp-config` with only {sessions, smriti, akhada},
  dropping the dead narada-speak). It becomes a THIN CLIENT of the brain
  server once the server exists.
- **MCP tools** — smriti (memory), akhada (fitness, `akhada.adapters.mcp_server`),
  prana sessions (`prana.sessions.mcp`). All live and proven (akhada logs
  from Telegram + voice today).
- **Voice worker** — `src/prana/voice/worker.py` (LiveKit + gpt-realtime).
  Separate path today; can later route through the brain server.
- **Host supervisor** — `~/.narada/host/components.yaml` defines the
  long-running components; the brain server becomes one of them.
- **Routing** — `src/prana/state/router.py` (`route_utterance`) and
  `presence.py` — NOTE both still point at the dead deha `127.0.0.1:8765`
  (migration debt; separate from the brain server, but relevant to the
  speak/presence layers).

## What NOT to redo

- Don't rewrite the Telegram bridge from scratch — it's already the
  proven Narada-over-Telegram path; make it a client, don't reinvent it.
- Don't build a second voice path — voice stays LiveKit/gpt-realtime.
- Don't build the akhada product or a cloud MCP — D5-gated.
- Don't touch narada-box3 — separate repo, a client, unchanged.
- Model already switched to `gpt-realtime-2.1-mini` for the box voice
  (cost); cost logging + akhada activity chips + graceful goodbye all
  shipped this session — don't re-solve those.

## Recommended first moves

1. **Run the cross-review** on the platform spec (Suti approved it) —
   `/cross-review` on `docs/plans/personal-agent-platform-2026-09-04.md`.
   It amends ADR-092's plan-of-record and defines cross-repo seams, so it
   should be validated before code. (Suti's workflow requires asking him
   once before starting; he already said yes to the review in principle.)
2. Then scaffold the brain server: pick Agent SDK vs Messages API, stand
   up `/v1/chat/completions` streaming, wire identity + MCP + one chat
   model, prove it end-to-end with a curl, then point the Telegram bridge
   at it.

## Parallel session

Suti kicked off a **narada-phone-app** session (the PWA client) in
parallel. Coordinate through the spec + the platform doc — don't edit its
repo from here.

## State / decisions from this session

Full detail in smriti `projects/prana` (2026-09-04 entries): platform
decisions, the deha-8765 migration debt, the Telegram-logging-works
correction, the akhada MCP pattern. Everything is committed and pushed
(all 5 repos; prana on branch `embodiment-rebirth`).
