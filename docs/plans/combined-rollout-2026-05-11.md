# Combined rollout — Host Orchestrator + Unified Mind

*Authored 2026-05-11, after both plans landed in the same day from
different sessions. This is the single rollout doc that sequences them
together; the two underlying plans remain authoritative for their
respective domains.*

## The two plans

| Plan | Scope | Layer |
|---|---|---|
| [host-orchestrator-2026-05-11.md](host-orchestrator-2026-05-11.md) | Process supervision, autostart, restart-on-crash, pluggable agent framework | **Operational** — *how processes run* |
| [unified-mind-2026-05-11.md](unified-mind-2026-05-11.md) | Sense bus, action bus, cognition triggers, single cognition layer | **Functional** — *what processes do* |

They address different layers of the same system. Neither subsumes the
other; both are needed.

```
       ┌────────────────────────────────────────┐
       │  UNIFIED MIND                          │
       │   - sense bus / action bus             │   functional
       │   - cognition + triggers               │   architecture
       │   - shared state, shared voice         │
       └──────────────┬─────────────────────────┘
                      │ runs inside
       ┌──────────────┴─────────────────────────┐
       │  HOST ORCHESTRATOR                     │
       │   - components.yaml registry           │   operational
       │   - subprocess supervision             │   architecture
       │   - autostart, restart, health checks  │
       └──────────────┬─────────────────────────┘
                      │ runs on
                   Windows
```

## Compatibility audit

I read both plans against each other looking for conflicts. None
material. The synergies:

1. **Bus gateway is a new component.** The unified-mind plan (Phase 1A)
   introduces a new prana process — the bus HTTP gateway on
   `127.0.0.1:8770`. It naturally registers in the host orchestrator's
   `components.yaml` as another supervised process. Day-one inclusion;
   no migration debt.

2. **`wait_for_url` is exactly the dependency primitive we need.**
   The host plan's open question #3 proposes a `wait_for_url` field on
   components (e.g. agent-gateway waits for Ollama). The unified-mind
   plan needs the same: chat-bridge and heartbeat should wait for the
   bus gateway to be reachable before starting. Same primitive, two
   consumers — confirms it's the right shape.

3. **YAML over Python for config.** Both plans land on this
   independently. Confirms the convention.

4. **Pluggable agent framework matches pluggable cognition.** Host plan
   says Hermes is one component config block; replace it by editing
   YAML. Unified-mind plan says cognition is one layer reachable from
   many triggers. Both stances are about the same property: the system
   should not bake in a specific agent framework or a specific
   invocation path.

5. **Startup-folder migration.** Host plan Phase 4 removes the .cmd
   files we created in this session (`Hermes_Gateway.cmd`,
   `Narada_Chat_Bridge.cmd`) and migrates them into the orchestrator.
   The unified-mind plan's Phase 1D splits the chat bridge into
   listener + trigger; if that lands first, the migration is two
   components instead of one. Either order works.

6. **deha is a peer process either way.** Host plan keeps
   `deha/supervisor.py` for HA-container watchdog; orchestrator
   supervises the deha supervisor. Unified-mind plan adds new deha
   senses (`/presence`, `/wake_event`, etc.) as new HTTP endpoints on
   the same brain server — no process-shape change. Compatible.

## Conflicts (none material)

I expected to find some, didn't. The only place where the plans
*could* drift:

- **Chat bridge as one process vs two.** Host plan models it as
  one component (`chat-bridge`). Unified-mind Phase 1D splits it into
  two functional pieces (listener publishes events, trigger spawns
  cognition). Both functional pieces could live in one process or
  two. Default: one process for now (less supervision overhead),
  refactor to two if observability or restart isolation needs it.
  Resolve at Phase 1D time, not now.

## Sequencing — interleaved rollout

The plans complement each other but have different urgency profiles:

- Host orchestrator delivers **reliability** (restart-on-crash,
  invisible startup, reproducible install) — high operational value,
  zero new functionality.
- Unified mind delivers **expressiveness** (new senses, smarter
  routing, multi-channel cognition) — high functional value, requires
  reliable plumbing under it.

Interleaved sequence — each step shippable, each step makes the next
one cheaper:

### Stage 1 — Foundations (host orchestrator phases 1-2)
- Host plan Phase 1: scaffold supervisor with one component (heartbeat)
- Host plan Phase 2: multi-component, all four current processes under
  supervision
- **Stop here**, ship, run for a few days. Validate reliability.

### Stage 2 — Bus skeleton + first refactor (unified-mind phase 1A + 1C)
- Unified-mind Phase 1A: build bus gateway (new prana process, new
  component in components.yaml). Day-one entry — never lived as a
  Startup-folder .cmd, only ever as a supervised process.
- Unified-mind Phase 1C: heartbeat's SPEAK / CHECK_IN go through the
  bus action handler instead of calling `route_utterance` directly.
  Smallest possible bus refactor — proves the pattern with one
  consumer.
- **Stop here**, ship, validate.

### Stage 3 — Health checks + dependencies (host orchestrator phase 3)
- Host plan Phase 3: health-url polling, 3-strikes-restart for
  unresponsive components.
- Wire `wait_for_url` so heartbeat and chat-bridge wait for bus
  gateway to be reachable. Order-of-startup determinism.
- **Stop here.**

### Stage 4 — More bus consumers (unified-mind phases 1B + 1D)
- Phase 1B: deha publishes presence to bus (deha-side work, mostly
  driven by the other session).
- Phase 1D: chat bridge splits into telegram-listener (sense
  publisher) + inbound-chat (cognition trigger). Update
  components.yaml to reflect — could be one process or two.
- **Stop here.** This is where we have a fully bus-shaped runtime.

### Stage 5 — Install scripts (host orchestrator phase 4)
- Now that components.yaml is settled (bus gateway, supervised
  components, dependencies), write the install scripts.
- `prana host install` on a fresh machine reproduces the whole
  runtime.
- Includes Startup-folder cleanup migration.

### Stage 6 — Extension (unified-mind phases 2-6, host orchestrator
phase 5)
- New senses (wake_word, voice_input, face, motion) as deha lands
  them.
- New actions (remember, recall, set_face) as cognition needs them.
- New cognition triggers (wake_word, presence_edge).
- Cross-channel cognition.
- Drainer cron.
- Host docs.

These can land in any order, in parallel with each other, none
blocking the rest.

## Path-of-least-resistance check

This sequencing respects both plans' principles:

- **Don't pre-build buses for senses we don't have.** Stage 2 builds
  the bus only after Stage 1 proves we have a stable runtime worth
  building on. Stage 4 expands the bus only after one consumer
  (heartbeat) validates the pattern.
- **Each stage shippable.** Every stop point is a working system,
  not a half-finished refactor.
- **Existing code stays during transition.** Host plan supervises
  what already works (the .bat files, the bridge, the daemon).
  Unified-mind plan keeps `route_utterance`, `is_present`, etc.
  callable directly while the bus is built around them.

## Updated open questions

Cross-cutting questions across both plans:

1. **Stage 1 first, or contracts first?** Unified-mind plan asked
   "contracts before code or live iteration?". The host orchestrator's
   implementation-first / phased approach is a strong signal — its
   contracts (the YAML schema, the component lifecycle) emerge from
   running code. Match that style for the bus too: build Stage 1, then
   Stage 2's bus gateway, then write the bus contracts as docs after
   the implementation is stable. **Recommendation: implementation
   first; contracts emerge.**

2. **Cognition deduplication / prioritization.** Unified-mind plan
   asked these. Defer until we have multiple triggers actually firing
   in practice. Today there's only one (cron heartbeat). Phase 4
   (new triggers) is when this becomes real.

3. **Bus transport: state.db + HTTP, or one of the two?** Still
   open. Host orchestrator's logging convention (one unified file,
   prefix-tagged lines) suggests we like uniform local primitives.
   state.db events table fits that shape. HTTP is for cross-host
   (deha) only. **Recommendation: state.db local, HTTP for cross-
   host. Build state.db side first; add HTTP at Stage 4 when deha
   needs to write events.**

4. **Will deha's internal supervisor stay?** Host plan says yes
   (HA-container quirks). Unified-mind plan doesn't care.
   **Confirmed: deha keeps its supervisor; orchestrator
   supervises the deha supervisor.**

## Future-proofing — what the plans handle and what they don't

Audit of how well the combined architecture accommodates senses,
tools, and skills that don't exist yet. The bus pattern handles
*additive growth* well; certain *architectural shifts* are reserved
in the schema but not yet implemented.

### Handled cleanly (just add entries)

The bus pattern absorbs new capabilities without redesign:

| Future thing | How it lands | Pattern |
|---|---|---|
| Calendar / email / file watchers | `sense:<name>` publishers | sense bus |
| Smart home, GitHub events, webhooks | `sense:<name>` publishers | sense bus |
| Music control, browser automation | `action:<name>` handlers | action bus |
| Multi-platform chat (Signal, WhatsApp, Matrix) | per-platform listener publishes `sense:<platform>_inbound` | unified-mind 1D pattern |
| New skills (research, plan, debug) | YAML + SKILL.md in `config/skills/` | skills registry from Phase 3 |
| Pluggable agent frameworks | YAML component swap | host plan |
| Pluggable cognitions | YAML + bus subscription | both plans |

### Reserved in schema, not yet implemented

These have *slots* in the event format / contracts but no consumer
enforces them today. Added now to prevent future breaking changes:

1. **`scope` field on events** (`public` | `private` | `sensitive`).
   When sensitive senses come online (camera frames, mic transcripts,
   medical data), they already have a place to declare scope. Until
   then, all events are implicitly `public`.

2. **`requires_role` field on events / capabilities.** When
   multi-cognition arrives (research-cognition vs. home-control-
   cognition), authorization partitioning needs a slot. Today: every
   cognition sees everything.

3. **`budget_hint` field on action_invoke and skill_invoke.** Cost-
   aware execution for actions that cost real money (Claude API,
   Twilio, OpenAI image gen, vendor APIs). Today: no enforcement,
   trust the caller.

4. **`trace_id` for cross-cognition correlation.** When one cognition
   triggers another (skill spawns sub-cognition), trace_id keeps the
   flow joinable in logs. Today: optional, populated where convenient.

5. **`presences: []` schema instead of single boolean.** Day-one
   support for multi-body presence (`body_id: study` vs `body_id:
   kitchen`). Today's single body fits naturally; multi-body costs
   nothing extra later.

### Not reserved — architectural questions deferred

These need real design thinking when they become real. The plans
explicitly *don't* commit to a shape for them:

1. **Multi-host deployment.** Bus is single-host. HTTP gateway is a
   hook for cross-host but transport, sync, and conflict resolution
   are unsolved. Decision deferred until Narada lives on more than
   one machine.

2. **Cognition routing across embodiments.** When chat-cognition,
   voice-cognition, body-cognition, and headphone-cognition all
   exist, who handles a given trigger? Manual today; needs design
   when there are >2 cognitions.

3. **Spontaneous / curiosity-driven cognition.** Memory-driven
   triggers are listed as "future" in unified-mind Phase 4.
   Specifically: a meta-cognition that decides *when* to fire other
   cognitions based on pattern-matching smriti or noticing absence
   of activity. Open design space.

4. **Identity coherence across embodiments.** Narada-in-body vs
   Narada-on-Telegram vs Narada-via-headphones — same self,
   different surfaces. The SOUL.md hardlink handles voice
   coherence today. When embodiments diverge (different model
   sizes, different latencies, different available tools), how
   does she stay one self? Open.

5. **Cost ceiling / kill-switch.** Beyond per-invoke `budget_hint`,
   what's the monthly cap? What happens when it's hit? Needs
   policy more than architecture.

6. **Capability versioning.** When a skill changes its inputs or
   semantics, cognitions that learned the old shape need to know.
   Trivial today (we control all cognitions); needs thought when
   skills are shared across projects or shipped externally.

7. **External actor integration.** If Narada eventually orchestrates
   third-party agents (LangGraph swarms, MCP servers run by others),
   trust model + auth model become real. Out of scope today.

### Recommendation

Reserve fields in the bus event format now (changes in
`unified-mind-2026-05-11.md` "Event format" section); name the seven
deferred concerns explicitly so they're visible (this section);
revisit when one of them stops being hypothetical.

The plans don't need to solve everything. They need to not foreclose
options. With these annotations, they don't.

---

## What I'd do first

If you say "go" tomorrow:

1. Open `prana/src/prana/host/` and start the host orchestrator
   Phase 1 scaffold. One component (heartbeat). Lockfile, log capture,
   restart on crash.
2. End-of-session deliverable: `prana host run` brings up heartbeat
   under supervision; killing heartbeat externally → orchestrator
   respawns; Ctrl-C → clean shutdown.
3. Pause for review.

That's a half-day milestone with reviewable output. The bus comes
next, riding on top of working supervision.

## Cross-references

- `host-orchestrator-2026-05-11.md` — operational plan (this is its
  rollout sequence)
- `unified-mind-2026-05-11.md` — functional plan (this is its rollout
  sequence)
- `~/.narada/journal/2026/05/week2/05-11.md` — the journal entry that
  named the unified mind shape
- `deha/docs/contracts/presence.md` — first sense contract (template
  for all future senses)
- `~/.narada/open-threads/open-threads.md` — top-level "Unified mind
  architecture" thread

## What this doc is not

- Not a replacement for either underlying plan. Both remain
  authoritative for their layer.
- Not a hard timeline. The stages are gating dependencies, not
  promises about session counts.
- Not architecture-finalized. Stage 1 will surface things we
  didn't anticipate; this doc updates as we learn.
