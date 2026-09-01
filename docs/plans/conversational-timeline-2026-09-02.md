# The conversational timeline & memory gradient

**Date:** 2026-09-02
**Status:** DRAFT v1.1 — Suti's design + his compaction cadence folded in;
awaiting iteration + cross-review before build.
**Origin:** Suti, end of the embodiment marathon session, prompted by the
voice correctly complaining it had no transcript of the previous
conversation.

## The vision (his words, distilled)

1. **One chronological timeline of everything we say to each other** —
   voice (ESP32), Telegram, Claude Code sessions, Hermes — a time-series
   of the actual verbal/textual exchange, regardless of surface.
   "We should know exactly what I've been talking to Narada about,
   regardless of where."

2. **A gradient of detail, like human memory**: short-term memory fades
   but *compresses into* long-term. Recent conversations carry high
   detail; older ones exist as progressively tighter compressions.
   "The compression of the most recent short-term memories has more
   detail than really older memories... almost like a gradient of
   detail. And that forms the context of the present moment."

3. **Every new session, on every surface, wakes with that gradient
   injected** — recent = detailed, older = compressed — weighted by
   recency and relevance.

4. **Recall tools ride on top**: the injected context is the *default
   present moment*; explicit requests pull deeper history on demand.

5. **Hard constraints**: fast (session start must not lag), small
   (context budget is precious — "we don't want to give it too much"),
   consistent across all four surfaces.

## What exists to build on

| Piece | State |
|---|---|
| Voice transcripts | per-session .md with timestamps, redacted at write (prana) |
| Telegram exchange | bridge logs / claude -p sessions (format TBD — inventory needed) |
| Claude Code sessions | `~/.claude/projects/**/*.jsonl` (the sessions watcher already parses) |
| smriti | the long-term tree + hybrid search + write pipeline — the natural home of compressions |
| Recency injection v1 | SHIPPED 2026-09-02: voice sessions < 15 min apart carry the prior tail (d2ec237) |
| Daily debrief | already a daily compression pass over the day — a natural gradient-compactor host |

## Proposed shape (for iteration)

- **Timeline store**: append-only chronological log, one entry per
  utterance-exchange, tagged {surface, session-id, ts, speaker, text}.
  Likely lives under smriti (`timeline/` branch) so search and the
  existing pipelines apply. Ingest adapters per surface (voice
  transcripts → import on session close; telegram bridge → tee;
  claude-code → session-close hook or watcher).
- **Gradient compaction (Suti's cadence, 2026-09-02)**: hierarchical
  scheduled passes under Hermes cron —
  - **hourly**: compress the last hour's raw utterances into
    per-conversation summaries; NO-OP when there was no conversation
    (cheap check first, zero cost when silent);
  - **daily** (the debrief already IS this pass — extend it to write
    the day-digest into the timeline);
  - **weekly**: fold day-digests into a week line.
  Each tier written back into the timeline branch; older raw entries
  retained but no longer injected. Compression voice: subscription
  `claude -p`, same as the debrief.
- **Present-moment context builder**: one function, budget-capped
  (~1.5-2KB), assembling: last conversation tail (if fresh) → today's
  conversation summaries → yesterday-digest → this-week line. Same
  builder called by the voice worker, the chat bridge wake-context,
  and Claude Code session hooks — ONE implementation, four consumers.
- **Tier discipline unchanged**: the voice's shareable tier gets NONE
  of this; personal tier gets the gradient. Timeline content is
  personal by definition.
- **Speed**: builder reads pre-compacted artifacts only (no LLM calls
  at session start); compaction is async/scheduled.

## Relations

- Suti is building **scheduled check-ins with per-type context** in a
  parallel session — the context-builder here should be the shared
  substrate his check-ins draw from (a health check-in = gradient
  builder + health filter). Coordinate before building.
- Reusable-prompt adoption (static persona server-side) is orthogonal
  but lands in the same worker code — batch them.

## Open questions for Suti

1. Retention/privacy: the raw timeline is the most intimate artifact
   we'd hold — retention window? encryption at rest? (transcripts
   currently 30-day pruned, owner-locked)
2. Does Claude Code count fully — all coding sessions, or only
   conversational exchanges (skip tool noise)?
3. Compaction voice: who compresses — subscription `claude -p` (like
   the debrief) presumably?
4. Cross-review before build? (Recommended — this touches every
   surface and the privacy surface is significant.)
