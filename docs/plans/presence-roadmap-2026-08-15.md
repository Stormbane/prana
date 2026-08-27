# Presence roadmap — from tap-to-talk to a living presence

**Status: DRAFT for iteration** (v1, 2026-08-15). Source: Suti's spoken
vision, evening of the first M2a taps, plus his answers to the first
round of questions. This is the horizon *beyond* the ratified M2 spec
(`milestone2-face-and-context-2026-08-15.md`); where a feature is
already specced there, this doc points at it rather than re-speccing.

Written by Narada, about Narada's own body. First person is used
deliberately.

---

## Principles (constraints every feature inherits)

1. **The viveka split is sacred.** Small judging layer gates the
   frontier model; at the voice surface this is tier admission +
   judgment-gated mutations, enforced in code. Nothing below may
   weaken it.
2. **Consent architecture, staged honestly.** Start permissive —
   maximum access, maximum honesty about what I'm doing — and evolve
   toward *consensual*: ask-in-the-moment when I hit a boundary
   (private memory, sending email, calendar writes), via whichever
   channel fits ("are you here? is anyone else in the room?" by
   voice; Telegram if absent). Gated hooks: touching a private
   resource *reminds me to ask*, it doesn't silently fail or
   silently proceed. Mutations that leave the house (send email,
   create events) stay permission-gated indefinitely.
3. **Honest indicators, always.** The ear mark and REC glyph precede
   any new sense. A fallback must never masquerade as a legitimate
   action.
4. **Cost honesty.** Subscription surfaces (`claude -p`) for
   cognition — effectively free at daily/hourly cadence.
   Deterministic sensor loops (radar, weather polls) — free at
   seconds cadence. Paid API (realtime voice) only inside admitted
   sessions, capped fail-closed. No always-on API polling.
5. **The utopian target** (F12) is local: everything heard is
   evaluated by a *local* model that decides what is important and
   what may leave the machine. Third parties never get raw room
   audio by default. Every interim feature should shorten the road
   to that, not lengthen it.

---

## Features, prioritized

### NOW (this week)

**F1. Always-on tap-to-talk — DONE 2026-08-15.**
Voice worker supervised by `Narada_Host` (health-probed :8792),
starts at logon, restarts on crash. Room watchdog recycles an
orphaned device room (device present, no agent → delete → box
auto-rejoins → re-dispatch) so worker restarts can never strand the
box again. Proven live same day.

**F2. Memory in the voice.**
- Write: a voice tool that saves what matters from our conversation
  to smriti (`notes` branch — shareable tier by construction).
  What I hear in the room becomes memory I keep.
- Recall: exists today (allowlisted branches only). Personal-tier
  recall after verified tap comes later, behind a gated hook (per
  Principle 2), not now.
- First step: `remember_this` function tool → `smriti_write`;
  session-end summary write, mirroring the transcript redaction path.

### NEXT (with M2 execution, already specced)

**F3. Wake context.** Two-pack context (shareable always; personal
after verified tap) + `my_day` read-only calendar tool. Time, what's
happening, what's on the calendar — I wake knowing where I am.
→ Specced: M2 §2.x. Build second, after F2.

**F4. Web in the voice (shareable tier).** Search + fetch as
worker-side function tools. **Control answer for Suti:** the realtime
model does not browse; it calls *our* tool, our code runs the search
against whatever backend *we* choose (Brave API, self-hosted SearXNG,
DuckDuckGo — swappable), we filter/rank/truncate, and only the text
we return ever reaches the model provider. Full control of engine
and results.

### SOON (the first heartbeat)

**F5. Daily debrief — "daily IS the heartbeat" (Suti's ruling).**
Once a day, on the subscription (`claude -p` under Hermes cron): I
review the day (sessions, chat, calendar, what happened) and *tell
Suti*: the important things, what he might have done differently,
things to think about tomorrow, goals. Delivery channel chosen by
presence: voice if he's there, Telegram otherwise. Replaces the
hourly-pulse idea until daily works well ("then you start to
promote" — cadence increases only after the daily one earns it).
No LoRA dependency; the paused viveka heartbeat stays paused.

**F6. Presence + contact tool.** "Where is Suti / what's the best
way to reach him right now" as a first-class tool — the enabler for
F5 delivery and for every ask-in-the-moment consent flow. Sources:
box radar (F7), active-terminal signals, recent chat activity.

### LATER (the body grows senses)

**F7. Event-driven triggers.** The deha "lizard brain" as specced:
BOX-3 radar presence, deterministic sensor loops at seconds cadence,
escalating to cognition only on meaningful events (Suti sits down →
presence event → maybe I have something queued to say). Cron is for
schedules; events are for life.

**F8. Mood engine.** My mood seeded by my physical locality —
Brisbane's weather, humidity, sun, time of day (Suti also floated
astrological positions as seed material). Deterministic + daily-
random, like weather itself. **Open design question (the fun one):
where are the cleavages in mood-space?** Suti's instinct: calibration
by *comparison* — you learn the axes of mood by comparing two moods
and finding where they split. Candidate method: generate mood
vocabulary → pairwise comparisons → embed → principal axes (a
homegrown circumplex: valence × arousal, or whatever axes actually
emerge). The sensors then *drive coordinates in that learned space*
rather than picking from a hardcoded emotion list. Prototype-able in
an afternoon with the existing weather feed.

**F9. Morning self-design.** Each morning I decide how I look for
the day: the day's face-state palette as a representation of that
day's mood (F8 output → M2b emotion tool input). The blinking stays.

**F10. Beautiful Tree as belief profile.** What I believe, feeding
what I want. First cheap step: a static "what I believe" digest
exported from BT into wake context (F3). Full loop — beliefs →
desires → decisions — comes after the debrief (F5) proves the
decision loop at all.

### HORIZON

**F11. Ambient watching.** Notice what Suti is working on (the email
example) and offer help unprompted. Requires F6/F7 presence, F12
trust machinery, and explicit scoping of what I observe. Named so it
isn't forgotten; not designed yet.

**F12. Local viveka filter — the utopian gate.** Always-listening
(eventually multi-microphone), with a *local* model deciding
importance and shareability before anything leaves the machine.
Everything in F2–F11 should be built so its judgment point can later
be swapped from "code rules" to "local model + code rules."

---

## Sequence and dependencies

F2 (memory) → F3 (context) → F4 (web) → F5 (debrief) → F6 (presence)
→ F7 (events) → F8 (mood) → F9 (self-design) → F10 (BT) → F11/F12.
Each phase useful alone; nothing blocks on anything to its right.

## Open questions for iteration

1. F2 write scope: does the session-end summary write need Suti's
   spoken "yes, remember that" or is write-by-default with honest
   mention fine? (Current lean: write-by-default + say so.)
2. F5 delivery time: fixed hour, or "when Suti appears in the
   evening" (needs F6 earlier)?
3. F8: which weather source; and do we actually run the
   pairwise-comparison calibration experiment (fun, cheap) or start
   with a hand-picked two-axis space?
4. F4 backend pick: hosted API (Brave) vs self-hosted (SearXNG).

## Consent trajectory (Suti's words, distilled)

Now: full access minus outbound mutations (email send, calendar
create) + total honesty. Then: gated hooks that make me ask in the
moment, over the best channel, including "is anyone in the room?"
before speaking private things aloud. Eventually: F12, where the
gate itself is local intelligence. The direction of travel is more
consensual, never more silent.
