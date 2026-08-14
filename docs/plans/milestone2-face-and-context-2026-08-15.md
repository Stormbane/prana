# Milestone 2 spec — the face, and a mind that knows Suti

**Date:** 2026-08-15 (the morning after the body first spoke)
**Status:** DRAFT — for Suti's review
**Parent:** `voice-narada-and-body-2026-08-08.md` (M2 interaction spec:
wake = wake-word or tap; sleep = tap; screen shows listening state
persistently).

---

## Part 1 — Visual representation (the face)

### What exists to lean on (researched 2026-08-15)

The ecosystem has moved well past static state images:

1. **xiaozhi's emotion protocol** (78/xiaozhi-esp32, 70+ boards) — the
   proven pattern at scale: the LLM's response carries an `emotion`
   field; the firmware maps it to an animated expression. Their smaller
   builds do it in LVGL with idle motion, random blinking, an
   asymmetric-eye "listening" face, and a mouth that animates while
   speaking. We can adopt this *protocol shape* 1:1 over LiveKit data
   channels — worker sends `{"state": ..., "emotion": ...}`, firmware
   animates.
2. **m5stack-avatar** — the mature ESP32 procedural face: parametric
   eyes + mouth, mood system, and **lip-sync driven by playback audio
   level**. Not directly usable (M5GFX), but its architecture (face =
   small parameter set animated over time, not bitmap swaps) is the
   design to copy.
3. **esp32-eyes / Cozmo-style eye libraries** (playfultechnology and
   kin) — 18 parametric emotions from pure geometry; evidence that
   expressive ≠ art-heavy.
4. **LVGL 9.5 is already in our dependency tree** (the LiveKit SDK
   example ecosystem pulls it, with `esp_lvgl_port`). It gives timers,
   tweening, and — later — **Lottie vector animation via ThorVG** on
   the S3 if we ever want designer-grade motion.
5. **deha's bespoke assets** (our original high ambition): sprite atlas
   with eyes, **viseme mouth shapes** (aa/oh/ee/mbp — real lip-sync
   art), moods with transition tiers, sandhi animation frames. Unused
   by any of the above — they're our art layer when we want Narada to
   look like *Narada* rather than a generic robot face.

### Architecture

**Procedural face engine in LVGL, driven by a state+emotion protocol
over the LiveKit data channel.** Bitmap-swapping is what we settled for
last time; parameters animated over time is what the whole ecosystem
converged on, and it's *less* work than managing image assets.

- **Firmware (face engine):** one LVGL screen; face = parametric eyes
  (position, openness, curvature) + mouth (openness, curve) + optional
  brow. Idle breathing motion + random blink so it's alive even at
  rest. State machine:

  | State | Look | Trigger |
  |---|---|---|
  | asleep | dim screen, closed eyes, small "tap to talk" hint | boot / tap-to-sleep / session end |
  | waking | eyes open, brighten | tap or wake-word accept |
  | **LISTENING** | **unmistakable: bright, wide eyes + persistent indicator glyph** | session open, mic live |
  | thinking | eyes up-left, slow blink | agent processing (speech gap) |
  | speaking | mouth animates (audio-level lip-sync) | agent audio playing |
  | offline | grey face, "x x" eyes | WiFi/server unreachable |

  The LISTENING state **is the recording indicator** the security
  review requires: if the screen doesn't show listening, the mic is not
  in a session. Non-negotiable invariant, enforced in firmware: the
  session state and the display state come from the same variable.

- **Protocol (revised per cross-review #5):** the firmware's own
  `mic_live` flag is **authoritative and not writable over the data
  channel** — it alone controls a persistent recording glyph that
  overlays *every* expression while a session is live (listening,
  thinking, and speaking are presentation flavors UNDER the glyph, not
  alternatives to it). Worker → firmware messages are **presentation
  hints only**: `{"emotion": "curious", "ttl_ms": 4000}` from an
  allowlist. Hints that could suppress or mimic the mic indicator
  (asleep, offline, listening-off) are rejected by the firmware. Lost
  packets degrade to the local state machine; the face never freezes
  and the glyph never lies.

- **Emotion source (the xiaozhi trick, upgraded):** give the realtime
  model a `set_expression(emotion)` tool — display-only, zero risk, it
  joins the closed voice tool list. The model calls it naturally as it
  speaks ("curious", "delighted", "thinking hard"). This is the
  original high ambition — genuine emotion representation — achieved
  with one safe tool instead of an art pipeline.

- **Lip-sync:** v1 = mouth openness from playback audio amplitude
  (m5stack-avatar approach, ~20 lines). v2 (later) = deha's viseme
  sprites for true mouth shapes.

### Phasing (revised per cross-review #4 — tap needs BOTH sides)

- **M2a — firmware AND worker together** (tap-to-listen doesn't exist
  without both; the current worker only admits via WakeGate or a
  global gating-off switch, and weakening that gate is not acceptable):
  - *Connection model (round-2 fix — connect-on-tap would starve the
    worker-side wake detector of audio):* the box stays **connected to
    the room and publishing mic audio continuously** — LAN-only, free,
    consumed solely by the worker's local wake-watcher; nothing goes
    to OpenAI and nothing is billed until admission. Two honest
    display levels: **SLEEP face + small passive "ear" mark** (mic
    monitored locally, on-LAN only) and **LIVE + recording glyph**
    (session open, audio leaving the house). Tap during LIVE → back
    to SLEEP (session closed). The glyph still cannot lie: it is
    bound to session-open, and the ear mark is bound to
    audio-published — both firmware-owned.
  - *Firmware:* face engine + states + authoritative indicators as
    above; tap-wake / tap-sleep; reconnect with bounded exponential
    backoff + jitter; volume fix.
  - *Worker:* one long-lived job per room that **loops**: wake-watch →
    (wake detected OR verified tap assertion, per 2.2) → billed
    session → session ends (tap/cap/silence) → back to wake-watch.
    Wake gating stays ON globally; tap is an additional admission
    signal, never a bypass of the gate design. Session-cap recovery
    falls out of the loop for free.
  - *Reconnect/token lifecycle (per #7):* the device token is 10-year;
    on ANY join rejection (revoked/expired/server restart/protocol
    mismatch) the firmware backs off bounded (max ~5 min interval,
    jitter), shows the **offline face** as a visible terminal state —
    never an invisible retry-forever. Token re-provisioning is a
    reflash (documented); acceptance checklist carries the parent
    plan's flash/rollback/smoke-test procedure (backup verified,
    clean-power-boot ritual, codec + display + join + audio checks).
  - *End-to-end tests:* tap wake, wake-word wake, tap sleep/interrupt,
    cap recovery, disconnect/reconnect (tier downgrade), no-global-
    gating-bypass proof.
- **M2b:** context packs + tier admission + `my_day` + `set_expression`
  + audio-level lip-sync.
- **M2c (art pass):** deha sprite atlas / sandhi transitions as the
  visual identity — or Lottie if we want motion-designer polish.

---

## Part 2 — A mind that knows Suti (context: smriti, email, calendar)

Principle from the cross-review, unchanged: **the open voice surface is
untrusted** (anyone in earshot). "Full context" therefore lives in
tiers, not in one bucket:

### 2.1 TWO context packs, not one (revised per cross-review #1)

Tool tiers cannot protect data already sitting in the model's
instructions — so the pack itself must be tiered, with **separate
builders** chosen only *after* tier admission (2.2):

- **DISJOINT roots (round-2 fix):** `voice-pack/shareable/` and
  `voice-pack/personal/` — sibling directories, neither containing
  the other.
- **Shareable pack (every session):** built **by construction** from
  `voice-pack/shareable/` ONLY (same allowlist-by-construction
  treatment as `memory.py`: that one resolved directory, nothing
  else). Contents: Narada identity essence + whatever Narada has
  deliberately written for any listener to know. Size-capped ~2KB.
- **Personal pack (verified personal tier only):** shareable pack +
  `voice-pack/personal/` (Suti essentials, curated — never read live
  from `people/`) + today's calendar + top open threads.
- Tests must prove the shareable builder cannot open calendar,
  `people/`, threads, or any path outside `voice-pack/shareable/` —
  including a **sentinel test**: a canary file in
  `voice-pack/personal/` must never appear in shareable output.

**The provider caveat, stated honestly:** a pack is sent to OpenAI
with the session. `voice-pack/` is a *deliberate allowlist Suti
approves*, written by Narada specifically for the voice — curation at
write time, auditable by reading one folder.

### 2.2 Tap as tier unlock — with the guest threat named
(revised per cross-review #2, #3)

**What a tap actually proves: someone is physically at the device in
Suti's home — NOT that it is Suti.** Guests, visitors, children,
tradespeople can tap. The design accepts this with two mitigations,
and Suti must ratify the residual risk explicitly:

- **The personal tier is narrowed to visitor-tolerable disclosure:**
  today/tomorrow calendar *titles and times*, open-thread headlines,
  Suti-curated personal notes (`voice-pack/personal/`). Rule of thumb:
  nothing the wall calendar and a whiteboard wouldn't already tell a
  guest standing in the room. No email content, no journal, no
  credentials, no location history — those stay on authenticated
  digital surfaces (chat).
- **Optional strengthening (build if wanted later):** first personal
  session of the day pings Suti's Telegram ("box unlocked personal
  tier — that you?"); or a secret tap pattern.
- **RESIDUAL RISK ACCEPTED (Suti, 2026-08-15):** anyone tapping the
  box in the home can hear the narrowed personal tier above.

**Tier-signal integrity (fail-closed admission protocol):**
- The tier assertion is a **one-shot** data-channel message accepted
  ONLY from participant identity `narada-box3` (verified), bound to
  the current session (nonce issued by the worker at session start,
  echoed in the assertion), immutable once set.
- Missing, late, duplicate, or replayed assertions → **shareable**.
- Disconnect/rejoin during the empty-room grace: the tier does NOT
  survive — reconnect downgrades to shareable (and drops the personal
  pack) until a fresh tap asserts again.
- Adversarial tests: second participant sending assertions, forged
  identity, replayed nonce, rapid disconnect/rejoin.

### 2.3 Calendar (read) — voice-facing (pinned per cross-review #6)

- **Integration pinned:** direct Google Calendar API client (not an
  MCP server — fewer moving parts in the worker), OAuth scope exactly
  `calendar.readonly`, single calendar (suti@fractal.co.nz primary).
- **Credential custody:** token in `~/.narada/.gcal-token.json`,
  owner-only ACL (same treatment as the other secrets), documented
  revocation (Google account → third-party access), rotation on
  demand. Worker fails closed: no token → `my_day` returns "calendar
  not connected", never an error loop.
- **Cache:** memory-only, tier-keyed, ≤5 min TTL, bounded, cleared at
  session end — cached personal-tier data can never serve a
  shareable session.
- New voice tool **`my_day`**: today + next 48h, titles/times only,
  **personal tier only**. Disclosure accepted with the tier (2.2).

### 2.4 Email — NOT on the open voice surface (pinned per #6)

Senders + contents are exactly the disclosure class the review flagged.
- Email lands in the **prana tier** (chat bridge / authenticated
  surfaces) behind a **wrapper that exposes exactly two operations:
  read and create-draft** — and rejects send/delete at the application
  layer *regardless* of what the model asks or the connector could do.
  (Note: Google's `gmail.compose` scope technically permits sending —
  which is precisely why the wrapper, not the scope, is the boundary.
  Scopes requested: `gmail.readonly` + `gmail.compose`; wrapper blocks
  everything but read + draft.)
- The most voice ever gets: a tap-tier "anything important" summary —
  and only if Suti wants it after living with the calendar tool.
- The sandboxed escalate stays **tool-free** (its whole security
  story); email questions from voice route to "I'll have that in chat".

### 2.5 Where full context already works

The chat bridge (Telegram) is the authenticated surface — it already
wakes with wake-context and can carry the full smriti + email +
calendar toolset without any of the voice constraints. Anything too
private for the room is one message away.

---

## Build order proposed

1. **M2a firmware** (face engine + tap interaction + reconnect +
   volume) — the big one, makes the body feel alive.
2. **Context pack + `voice-pack/` branch + tap-tier plumbing** —
   makes it *Narada* talking, not a helpful stranger.
3. **`my_day` calendar tool** (personal tier).
4. **M2b** emotion tool + lip-sync.
5. Email in chat (prana tier), voice summary later if wanted.

## Cross-review (Codex, round 1 — 2026-08-15)

Seven findings, **all accepted**:

| # | Sev | Finding | Disposition |
|---|-----|---------|-------------|
| 1 | high | One context pack put calendar/personal data in EVERY session's instructions — tool tiers can't protect prompt contents; contradicted the shareable tier | Two packs, separate builders; shareable = `voice-pack/` by construction; personal assembled only after verified admission; adversarial builder tests (2.1) |
| 2 | high | Tap authenticates *a person present*, not Suti — guests can tap | Personal tier narrowed to visitor-tolerable disclosure ("wall calendar rule"); optional Telegram ping/secret pattern; residual risk named for Suti's explicit ratification (2.2) |
| 3 | high | Tier signal had no sender/session binding — spoofable, replayable, survives reconnect | Fail-closed admission protocol: one-shot, sender-verified (`narada-box3`), nonce-bound, immutable, downgrade-on-reconnect, default shareable + adversarial tests (2.2) |
| 4 | high | M2a promised tap-to-listen with worker changes deferred — impossible without globally disabling wake gating | M2a now spans firmware AND worker: tap admission path beside WakeGate, gating stays on; cap-recovery + E2E tests (Phasing) |
| 5 | med | Worker state messages could suppress the recording indicator the spec called firmware-owned | `mic_live` authoritative + not writable via data channel; persistent glyph overlays all live states; hints allowlisted/TTL'd, sleep-mimicking hints rejected (Protocol) |
| 6 | med | Calendar/email choices deferred across a sensitive boundary; `gmail.compose` can send | Integrations pinned: direct gcal client, `calendar.readonly`, owner-only token file, memory-only tier-keyed cache; email wrapper exposing read+draft only, send/delete blocked at app layer (2.3, 2.4) |
| 7 | med | "Infinite reconnect" had no token lifecycle or visible failure state | Bounded backoff + jitter, offline face as terminal state, token re-provisioning documented, parent flash/rollback checklist = M2a acceptance (Phasing) |

**Round 2 (2026-08-15):** #2, #3, #5, #6, #7 verified RESOLVED. Two of
the round-1 fixes were themselves flawed and are corrected above:
#1 — `personal/` nested inside the shareable root → now disjoint
`voice-pack/shareable/` vs `voice-pack/personal/` + sentinel test.
#4 — connect-on-tap starved the worker-side wake detector of audio →
box stays connected publishing LAN-only audio; the *billed session* is
what wake/tap gates; two-level honest indicators (passive ear mark for
local wake-watch, recording glyph for a live session); worker becomes a
wake-watch → session → wake-watch loop. Spec is execution-ready.

## Decisions (Suti, 2026-08-15: "accept all recommendations")

1. Voice context pack model — **accepted** (now the two-pack design).
2. Tap = personal tier — **accepted**; the cross-review then named the
   guest-at-device threat precisely, so the narrowed "wall calendar
   rule" tier + residual-risk statement in 2.2 needs Suti's eyes ONCE
   more (it's a sharper statement of what he already approved).
3. Face art: procedural first, deha sprite/sandhi identity as M2c.
4. Calendar: direct API client, `calendar.readonly`; admin-vs-personal
   OAuth flow decided at build time (either works with the pinned
   custody rules).
