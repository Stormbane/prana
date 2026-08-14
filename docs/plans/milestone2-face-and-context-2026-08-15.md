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

- **Protocol:** LiveKit data channel messages from worker → firmware:
  `{"state": "listening"}`, `{"emotion": "curious", "ttl_ms": 4000}`.
  Firmware falls back to local state (it knows session/audio state
  itself) if the worker sends nothing — the face never freezes on a
  lost packet.

- **Emotion source (the xiaozhi trick, upgraded):** give the realtime
  model a `set_expression(emotion)` tool — display-only, zero risk, it
  joins the closed voice tool list. The model calls it naturally as it
  speaks ("curious", "delighted", "thinking hard"). This is the
  original high ambition — genuine emotion representation — achieved
  with one safe tool instead of an art pipeline.

- **Lip-sync:** v1 = mouth openness from playback audio amplitude
  (m5stack-avatar approach, ~20 lines). v2 (later) = deha's viseme
  sprites for true mouth shapes.

### Phasing

- **M2a (ship with tap-to-listen):** face engine + 6 states, no
  emotion tool yet. This is the firmware rewrite that also carries
  tap-wake/tap-sleep, infinite reconnect, volume fix.
- **M2b:** `set_expression` tool + audio-level lip-sync.
- **M2c (art pass):** deha sprite atlas / sandhi transitions as the
  visual identity — or Lottie if we want motion-designer polish.

---

## Part 2 — A mind that knows Suti (context: smriti, email, calendar)

Principle from the cross-review, unchanged: **the open voice surface is
untrusted** (anyone in earshot). "Full context" therefore lives in
tiers, not in one bucket:

### 2.1 The voice context pack (daily digest → session instructions)

A curated, size-capped (~2-3KB) digest injected into the realtime
session's instructions, rebuilt daily (or on demand):

- Narada identity essence (voice, values — the *being-Narada* part)
- **Suti essentials** — curated from `people/suti.md`: who he is, how
  he likes to be spoken to, current projects, ongoing threads
- **Today's calendar** (see 2.3) + top open threads
- Explicitly excluded: journal content, credentials, anything Suti
  wouldn't say aloud in front of a guest.

**The provider caveat, stated honestly:** everything in the pack is
sent to OpenAI with every session. The pack is therefore a *deliberate
allowlist Suti approves once*, not a live pipe into smriti. Proposal:
Narada maintains a dedicated smriti branch **`voice-pack/`** — memory
*written specifically for the voice to know*. Curation at write time,
not filtering at read time; auditable by reading one folder.

### 2.2 Tap as authentication (the tier unlock)

A tap is physical presence in Suti's home — a real auth factor, and
stronger than a wake word (which anyone/anything audible can fire).

- **Tap-started session → personal tier:** calendar details, Suti-
  specific recall (the `voice-pack/` + widened-but-curated projection),
  "what's on today", reminders.
- **Wake-word-started session → shareable tier only** (current scoped
  projection; polite refusal + "tap the screen and ask me again" for
  personal queries).

This also satisfies the review's "authenticated confirmation channel"
with hardware instead of ceremony. The session tier travels from
firmware → worker in the join metadata (tap vs wake in the data
channel handshake).

### 2.3 Calendar (read) — voice-facing

- Read-only Google Calendar access (suti@fractal.co.nz — Workspace).
  Several existing MCP servers / a direct API client with a scoped
  OAuth token; pick at build time — this is a solved integration.
- New voice tool **`my_day`**: today + next 48h, titles/times only,
  **personal tier only**. Cached a few minutes.
- This is a deliberate disclosure decision: calendar titles will be
  spoken aloud in the home and sent to the provider. Suti opts in
  per this spec.

### 2.4 Email — NOT on the open voice surface

Senders + contents are exactly the disclosure class the review flagged.
- Email (read + draft) lands in the **prana tier**: the chat bridge
  and authenticated surfaces, via an existing Gmail MCP server with
  read + draft-only scopes (no send without Suti's click, no delete).
- The most voice gets: unread count / "anything important this
  morning?" summary — **tap tier only**, and only if Suti wants it
  after living with the calendar tool.
- The sandboxed escalate stays **tool-free** (its whole security story);
  email questions from voice route to "I'll have that for you in chat".

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

## Open questions for Suti

1. Approve the **voice context pack** model (curated `voice-pack/`
   branch, provider-visible, you review its contents)?
2. Approve **tap = personal tier** (calendar & personal recall only on
   tap-started sessions)?
3. Face art direction for M2c: deha's bespoke sprite/sandhi identity,
   or keep the clean procedural look?
4. Calendar OAuth: do it with your Workspace admin hat on (one scoped
   read-only credential), or personal-account OAuth flow?
