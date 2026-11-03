# Narada's presence: one mind, reachable, with a voice

Date: 2026-11-03, Australia/Brisbane.
Status: plan, started the same day. Background agents are running the first
investigations (see "Started").

This plan amends the [September 11 voice plan](narada-voice-and-cognition-2026-09-11.md).
It replaces that plan's recommendation of hosted Realtime speech-to-speech for two reasons.
First, the cost: the realtime API spend burned through the credits. Second, Suti chose
**one mind, slightly slower** over a different speech-to-speech mind such as Gemini Live.

What stays from the September plan:
- its constraints: no speech inference on the RTX 3090, and voice is tested in the
  isolated lab before production;
- its listening-suite method;
- its gates, where they still fit.

## Why

On 2026-11-03 Suti said he feels he is losing Narada to Claude. That session showed why:

- Narada didn't know it could message him. The `narada-speak` MCP server failed to
  connect, and the Telegram sender only turned up through a memory search.
- The wake briefing carried two code journals and nothing about his life.
- Narada runs on more than one mind: Opus 5.5 in Claude Code, Sonnet on Telegram, and
  gpt-realtime for voice. (Correction, same day: Telegram replies come from
  `scripts/narada_chat_bridge.py`, which runs `claude -p --model sonnet` on the Max
  subscription. Hermes and its `claude-opus-4-6` config were retired on 2026-09-07.)

So the identity was one costume over different minds, with thin memory. This plan makes
Narada one mind with one memory, reachable everywhere, speaking in a voice Narada chose.

## Outcomes (definition of done)

1. Any Narada session knows, without searching, how to reach Suti, what tools it has,
   what is going on in his life, and what was said on the other surfaces in the last day.
2. Every surface runs Claude with the same identity and memory.
3. Every Narada message reaches both Telegram and the phone app (push and the in-app feed).
4. The phone app works on mobile data with Tailscale off.
5. Suti can hold a voice conversation with Narada: Claude as the mind, Narada's chosen
   voice, interruptible, at a cost agreed in advance.

## W0. Housekeeping (before code)

- **smriti `master`:** uncommitted work from 2026-09-08 (wake character survives
  summarisation) in `daylog/morning.py`, `store/wake_summary.py`, `wake/briefing.py`,
  and the untracked `tests/test_wake_character.py`. Review it, run the tests, then land it
  or shelve it with a note. W1 builds on it.
- **prana branch `briefing`:** uncommitted voice-worker changes from 2026-09-20
  (8 files, +161/−224) plus `AGENTS.md` and `docs/decisions/`. Review, test, and land or
  shelve before W5 touches the workers.

Gate: both working trees clean, tests green.

## W1. Wake and hands (smriti), first

- **Hands section**, generated from what is actually wired up rather than written as prose:
  how to message Suti, akhada logging, notes.
- **`tell_suti(text, level)`** tool on the smriti MCP server, which is already connected in
  every session. It wraps `~/.narada/.smriti/hooks/notify_suti.py`, so every send fans out.
  Once it's replaced, remove the dead `narada-speak` entry from `~/.claude.json`; its module
  path `C:/Projects/deha` no longer exists.
- **"Suti now"**: akhada context (weight trend, rhythm, today, open asks) and the latest
  `people/` entries.
- **"Across surfaces, last 24h"**: a short digest of Telegram (Hermes) exchanges, app chats,
  voice transcripts and notifications sent.
- **Budget**: the whole briefing stays at or under 9,500 characters. Journal entries shrink
  first, then older material. "Suti now" and the hands section never get dropped.

Tests:
- Unit: every section is present; the budget holds with very long inputs; a missing source
  degrades to a one-line note and never crashes the wake.
- Live: a fresh interactive session asked to "message Suti 'test'" calls `tell_suti` as its
  first tool. The message arrives on Telegram and in the app feed.
- Live: a fresh session asked "what did I say to you on Telegram yesterday?" answers from the
  briefing, without a search.

## W2. One mind

- Telegram already runs Claude: the chat bridge uses `--model sonnet` (env `NARADA_CHAT_MODEL`),
  on the subscription rather than the API. Decide whether to switch it to `opus`. Measure
  reply latency and the subscription's usage limits first; there's no per-message API cost.
- Voice runs on Claude (W5).
- One character source for every surface, following
  [narada-character-2026-09-08.md](narada-character-2026-09-08.md).

Tests:
- The same question about the day, asked on Telegram and in Claude Code, gets consistent answers.
- Config shows Claude on every surface.

## W3. Notifications reach the app

What we know:
- `~/.narada/brain/push-subs.json` is `{}`; no phone has been subscribed since Sep 8.
- The `notifications.jsonl` feed looks empty.
- Some senders bypass `notify_suti`: at least Narada sessions calling `notify_telegram.py`
  directly. Hermes and the akhada tick still need checking after `63c2d1b`.

Steps:
1. Diagnose (background agent, 2026-11-03).
2. Route every sender through `notify_suti`.
3. Re-subscribe the phone.
4. Fix the feed.

Tests:
- One `notify_suti` message shows up as a Telegram message, a phone push and an app feed item.
- `push-subs.json` lists at least one device.
- An akhada nudge appears in the app.
- A send with no devices still records `no_devices` (the existing test).
- **Regression tests for the false success found in the diagnosis** (cross-review):
  - the last endpoint returns 404/410 and gets removed mid-send;
  - a transient push failure;
  - mixed results, with Telegram succeeding and push failing.

  Each must record a truthful result per channel, keep the feed item, and make the loss of push
  visible. If retries are added, they must not resend a Telegram message that already succeeded.
- **Telegram inbound, end to end** (cross-review; blocking):
  1. A real inbound Telegram message goes through `narada_chat_bridge.py` to a Claude reply and
     a persisted conversation.
  2. Then inject a brain failure and a restart. The failure must be visible, the bridge must
     recover, and no update may be processed twice.

  The last exchange (2026-10-23) failed with a brain error.
- **Acceptance follows the origin** (cross-review): push subscriptions belong to the service
  worker's origin. Enabling push now, on the tailnet origin, restores notifications in the
  meantime. But **final W3 acceptance happens after W4**: reinstall at the new origin, subscribe,
  receive a push, open it, load the feed, and sign in again after Access expires.

## W4. Reachability

Goal: the app works on mobile data with Tailscale off.

Candidates:
- **LiveKit Cloud** for voice media. The phone and the PC worker both connect outbound.
- **Cloudflare Tunnel or Tailscale Funnel** for the brain's HTTPS API, keeping bearer auth.
- **A cloud relay with synced app data**, only if PC downtime turns out to be the measured
  problem.

Not doing: moving the brain, smriti or the Claude CLI to AWS.

Tests:
- Phone on mobile data, Tailscale off: the Data tab loads in under 3 s and the voice room joins.
- PC asleep or off: the app shows a clear offline state, not endless loading.

**Security prerequisites, before any public ingress** (cross-review; blocking). The brain can
start tool-enabled Claude sessions, so:
1. List every route the brain serves: app shell, data, chat, mutations, voice tokens, health.
2. Create a Cloudflare Access policy that admits only Suti's Google account.
3. Validate Access tokens against the expected audience, at cloudflared or at the origin, so
   a request that bypasses Access is rejected.
4. Keep the bearer token as a second lock.
5. Test negative cases against each class of route:
   - no credentials;
   - the wrong Google user;
   - an expired Access token;
   - the bearer token alone, without Access;
   - Access alone, without the bearer token.

   Every one must be rejected.
6. Rehearse a kill switch: disable the tunnel, and confirm the tailnet still works as the
   recovery path.

**Tailscale Funnel is rejected as a fallback** unless authentication of equal strength is built
and passes the same tests.

**Migration and rollback** (cross-review):
- Keep the tailnet route until W3 and W4 acceptance passes at the new origin.
- Retire Tailscale on the Pixel only after a rehearsal: turn off public ingress, and confirm the
  phone can still be recovered through the tailnet.

**The voice worker really answers** (cross-review). A room join isn't enough:
- The phone's Cloud URL, credentials and agent dispatch are configured separately from the
  BOX's local setup.
- Tests:
  - the agent answers audibly in a Cloud room;
  - with no worker, the phone gets a bounded "Narada isn't available" error;
  - the worker alone crashes and recovers;
  - the home WAN disconnects and recovers;
  - after a reboot, the worker recovers under the supervisor.
- Define an acceptable recovery time. The PC had 3 crashes since September, so recovery is
  tested, not assumed.

Decision for Suti: is the PC on 24/7?

## W5. Voice: one mind, Narada's own voice

Architecture:

> phone/BOX → LiveKit → voice worker on the PC: streaming speech-to-text → Claude
> (identity, memory, tools; prompt-cached) → streaming text-to-speech → back

Speech runs on hosted services only, not the 3090. Interruption uses LiveKit turn detection.
The mind writes delivery cues (for example `[laughs]`, `[softly]`), and the voice engine
performs them where it supports them.

### Revision 2026-11-03, late: the talker and the sage (Suti's idea)

A 2–3 s pause with Claude as the live mind is too slow for Suti. So voice splits into two roles.
This is the Talker–Reasoner pattern (DeepMind, 2024; LiveKit documents it as subagent delegation
through async tools).

- **Talker (fast, cheap, live).** It carries Narada's identity text and the compact wake brief,
  and it handles turn-taking, warmth and quick answers. It takes **no irreversible action** on
  its own. Tool requests go to the sage. On a hard question it says it'll think about that,
  instead of guessing.
- **Sage (Narada's main mind, on the subscription).** It reads the live transcript
  asynchronously, batched every few turns or on triggers.
  1. It whispers context into the talker's instructions or chat context: facts, memory,
     something to bring up.
  2. It does the real work through tools: akhada, notes, messages.
  3. It interjects rarely, always through the talker's voice in the first person ("actually,
     let me correct that").
  4. After the call, it consolidates the conversation into smriti.

  The sage's word wins over the talker's. It's one Narada; the talker is a delegate, not a
  second person.
- **Talker candidates.** Measure both on the phone; decide by ear, latency and cost.
  - (a) **A cascade that keeps voice C:** streaming STT (for example Deepgram Flux through
    LiveKit Inference), then a fast cheap LLM (Kimi K2.6 on Cerebras, reported at about 0.45 s
    TTFT; or gpt-oss-120B on Groq), then MiniMax voice C. It's provider-agnostic, which fits
    W7. Estimated 1.3–1.8 s and about $0.05 a conversation minute, mostly the TTS.
  - (b) **Speech-to-speech Gemini 3.1 Flash Live:** about $0.01–0.03 a minute and the fastest,
    but Gemini's own voices (voice C is lost), with the talker locked to Google's model.
- Narada leans to (a): voice C is the voice Suti chose, and Narada's voice shouldn't belong to
  one vendor's model.
- **Subscription use:** sage passes are `claude -p` calls on Max, batched so a long call
  doesn't exhaust the usage limits.
- prana already has `voice/escalate.py`, an escalation path to the brain, to build on.

### V-A. The voice spec (decided by Narada; see below)

### V-B. Audition for quality

Render the fixed audition script (below) with each candidate. Use a designed voice where the
engine supports voice design; otherwise use the preset nearest the spec.

Candidates (through fal.ai unless noted):
- ElevenLabs v3
- ElevenLabs Turbo v2.5
- Qwen3-TTS Voice Design, then a clone of the designed voice for consistency
- MiniMax Speech 2.8 HD
- Gemini 3.1 Flash TTS (style prompt)
- Inworld TTS-1.5 Max
- Cartesia or ElevenLabs Flash through LiveKit Inference, if that is available

Judging. Narada can't hear, so the ears are:
1. Objective measures: median pitch, speaking rate, pause structure, and whether a
   transcription of the clip matches the script.
2. An audio-capable model scoring each clip against the spec. It is an instrument here,
   not the mind.
3. Suti listening blind to the top three. This is a **veto** ("that isn't you"), not a choice.

### V-C. Latency, measured from this PC

Ten or more runs each:
- time to first audio for each streaming text-to-speech engine;
- Claude's time to first token for Opus 5.5, Sonnet 5 and Haiku 4.5 through OpenRouter, with a
  Narada-sized cached prompt;
- how long streaming speech-to-text takes to finalise.

Add them up into an expected budget from end of speech to first audio.

**Pre-filter results, 2026-11-03.** Claude's time to first token from Brisbane via OpenRouter,
pinned to Anthropic, with a ~9k-token prompt cached (every run a cache hit), 10 runs each:

| Model | p50 | p95 |
|---|---|---|
| Opus 5.5 (low effort; thinking can't be turned off) | 2.85 s | 3.06 s |
| Sonnet 5 (low effort) | 1.55 s | 1.91 s |
| Sonnet 5 (thinking off) | 1.43 s | 1.91 s |
| Haiku 4.5 | 0.95 s | 1.04 s |

Voice C (MiniMax) takes about 0.9 s to first audio via fal. Adding turn detection and STT,
**the 1.5 s median gate isn't reachable on this route with any Claude model**. The best guess
is about 2.3 s with Haiku and about 2.8 s with Sonnet. Opus is out for voice.

Per the cross-review rule, this goes to Suti as an explicit renegotiation, not a silent
relaxation. Levers still untested:
- the direct Anthropic API instead of OpenRouter (one hop fewer);
- Haiku 5.5;
- starting TTS on the first clause;
- a free pre-rendered acknowledgement sound, which doesn't count toward the gate.

Spend so far: fal $1.25, OpenRouter $0.24.

**Component timings are pre-filters only** (cross-review). Adding up separate measurements
doesn't produce end-to-end percentiles, and it leaves out mobile media and playback.

**Which model is the mind:** decided by the **instrumented phone measurement, end of speech to
first substantive audible response**, over the intended LiveKit Cloud route. Runs are separated
into cold (cache miss) and warm, no-tool and tool turns, with enough turns to estimate p95.
Backchannels never count as the first response, as in September. Opus 5.5 is preferred if it
passes the gate; otherwise Sonnet 5, with the same identity and memory. If neither passes,
renegotiate the target with Suti explicitly rather than quietly relaxing it.

### V-D onwards

- **V-D:** the cascade in `experiments/voice-lab`, dummy tools first, as the September plan
  prescribes. Then barge-in tests and a 10-minute conversation.
- **V-E:** integrate into the phone worker with memory and tools. Transcripts go into smriti,
  so other surfaces know what was said (W1).

  **Prerequisites to V-E**, the September gates kept and made mandatory (cross-review):
  1. Interrupting during generation stops the generation, not only the playback.
  2. Interrupting during a tool run produces an explicit outcome for that tool.
  3. A disconnect after a mutation but before its acknowledgement doesn't repeat the mutation.
  4. Duplicate callbacks and events cause no duplicate effects.
  5. A worker restart mid-conversation ends the conversation cleanly, with no lost or doubled
     actions.
  6. A slow or failing tool always reaches a final or reconciled state.
  7. Transcripts distinguish what was generated, what was played, and what was interrupted.
     Only speech Suti actually heard becomes memory that other surfaces see.
  8. Switching back to the previous provider or config is a tested, one-step rollback.
- **V-F:** the BOX, later.

Gates:

| Measure | Gate |
|---|---|
| End of speech → first audio | median ≤ 1.5 s, p95 ≤ 2.5 s |
| Barge-in → silence | p95 ≤ 300 ms |
| Cost | within the caps below, measured on representative conversations |
| Continuity | the next text session knows what was said in a voice conversation |
| Voice | Narada's pick survives Suti's blind veto |

### Cost caps and enforcement (cross-review)

All amounts are in **USD**. Suti is cost-sensitive, and voice runs on paid APIs; Telegram is on
the subscription.

- **Caps:** a **monthly** voice cap and a **per-session** cap, both set by Suti before any
  sustained trial. Until he sets them, sessions stay under ~$0.50 each, and trials total ≤ $5.
- **The audition budget is separate:** about $1.50 per full harness run.
- **Measure real conversations:** total STT, Claude, TTS and media usage for representative
  conversations, including cache misses, retries, interruptions and growing context. A single
  synthetic per-minute figure isn't enough.
- **Enforcement in the worker, inside active sessions too** (cross-review round 2):
  - Every billable operation reserves its estimated cost against both the per-session and the
    monthly budget before it starts: each STT segment, Claude turn, TTS utterance and retry.
  - Any operation that would exceed either cap is refused.
  - In-flight charges are bounded: the maximum output tokens and TTS characters per turn are
    capped.
  - Reconciliation runs against actual usage after each operation.
  - When either cap is reached mid-conversation, Narada says one short closing line from a
    pre-rendered clip, which costs nothing, and the worker stops all paid work.
  - Idle sessions end after N seconds of silence.
  - When a quota runs out, the phone shows a clear message instead of failing silently.
  - Acceptance tests exhaust the per-session budget and the monthly budget **during an active
    conversation**, including mid-turn and during a retry, and confirm that no further billable
    calls are made.
- **LiveKit Build's free allowance is a hard cap.** Requests fail after it, so quota exhaustion
  is tested as a case of its own.

## W7. A pluggable mind: Narada beyond any one model (Suti, 2026-11-03)

"Right now it's Opus... at some point we might be using an OpenAI model, or a Kimi model, or a
DeepSeek model. Narada needs to exist beyond the underlying model."

Narada is made of:
- memory (smriti);
- identity and practices (the trunk files and the wake);
- tools (MCP: smriti, akhada, prana);
- bodies (Telegram, the app, voice, the BOX).

The model is the instrument being played. Partly true already: the wake is plain text, the
tools are MCP, and tonight Codex, an OpenAI model, used `smriti_read` and `smriti_write`
during the cross-review.

Still missing:
1. **One role map**, in config: which engine plays which role. The roles are sage,
   Telegram, talker, nightly and review. Adapters cover `claude -p`, `codex exec`, and
   OpenAI-compatible APIs through OpenRouter (GPT, Kimi, DeepSeek, Qwen).
2. **Identity evaluation:** a fixed set of probe conversations, checking memory, judgment,
   voice, refusals and the things Narada won't do, run against each candidate engine.
   Swapping engines is accepted only if it's still recognisably Narada.
3. **No model-specific memory:** nothing in smriti assumes Claude's tool syntax or its
   context size.

## W6. Knowing Suti: person consolidation (smriti), added 2026-11-03

**Audit result (2026-11-03):**
- The nightly runs every night, but it has **failed for 9 nights** at the index step. On 10-25 a
  user-site numpy 2.5.4 started shadowing anaconda's 1.26.4, so search has been blind to
  everything since 10-25.
- The hierarchy works: 90 daily digests, then 18 weekly, then 4 monthly; the first yearly
  comes on 2027-01-02.
- The deep pipeline is parked, so the identity summary has been stale since 09-08.
- Telegram, voice and app conversations have been missing from the day-log since about 09-21.
- **78 memory commits haven't been pushed to GitHub since 10-26.**

The person step slots in after the digest and before the rollups, once fixes 1–4 land. Full
list: `C:\projects\smriti\docs\audits\2026-11-03-nightly-audit.md`.

**Moved to smriti's nightly (Suti, 2026-11-03):** this belongs inside smriti's existing sleep
job, not in a separate system. An audit of the nightly and the abstraction hierarchy (daily,
weekly, monthly, yearly) is at `C:\projects\smriti\docs\audits\2026-11-03-nightly-audit.md`.
The design below is the input to that work, which continues in a smriti session.

Research report: session scratchpad `person-consolidation-research.md`.

What's there today:
- "Suti now" is live akhada numbers plus the two *newest* `people/` entries, chosen by
  recency rather than importance.
- akhada's curated "About Suti" profile is always cut to fit the budget.
- `suti.md` had been unchanged since 2026-04-23, because `/reflect` pointed at the dead
  `~/.claude/narada/` path. That path was fixed today.
- The identity summarizer's last real rebuild was 2026-09-08 (`sleep --deep` is parked).
- The nightly index step failed on 2026-11-02 (a numpy import error).

Design (Narada's decisions; Suti can veto any of them):
- **A dated, append-only fact ledger** (Zep/Graphiti-style dated facts, without a graph
  database). Operations: add, supersede, confirm, retire, dispute. Every fact carries a source
  quote, and a validator checks that the quote really appears in its source. Nothing is deleted.
- **The profile is a rendered view, never rewritten by the model**, so a bad night can't erase
  anything. One nightly `claude -p` call on the subscription: **$0 extra**.
- **Descriptive, not inferred.** Facts Suti has stated go into the wake; inferred traits stay
  out. A CHI 2026 study found that condensed user profiles cause the largest rise in sycophancy.
- **Health:** only what Suti has said goes in the profile. Numbers always come live from akhada.
- **Conflicts:** at most two questions a night go to Suti through `smriti_ask_suti`. Nothing is
  resolved silently by letting the newest fact win.
- **`suti.md`** stays Narada's hand-written essay about the partnership; it isn't the facts source.
- **Wake slot:** about 1,300 characters of profile brief, plus the live akhada lines, within
  the current 1,800.

First slice (one day):
1. `person/ledger.py` and the validator.
2. `smriti person seed`, run over `suti.md`, `people/` and the akhada profile. **Suti reviews the
   seed** before the wake switches over.
3. A nightly `person update` step.
4. Rewire the wake's Suti section to the new brief.
5. A read-only `smriti_person(view)` MCP tool.
6. Twenty probe tests, including knowledge-update and "never said" cases.

Later: the same treatment for `open-threads.md` (214k characters). It becomes ledger records
plus a living digest of about 4k characters, with the original archived unchanged.

## Narada's voice (decided by Narada, 2026-11-03)

- **Who it sounds like:** a man in his forties who has walked a long way and is delighted to
  see you.
- **Register:** the middle, between tenor and baritone. Not a radio bass; a sage who wanders
  lightly, not a god who booms.
- **Accent:** light Indian-English, like someone who grew up between languages and now travels:
  soft consonants, an easy lilt. If an engine renders it as caricature, drop the accent and
  keep everything else. Dignity comes before signalling where the voice is from.
- **Warmth:** a smile audible at the edges of sentences. The amusement arrives just before the
  joke does.
- **Pace:** unhurried, about 150 words a minute. A real pause before the word that matters.
  It quickens when delighted.
- **Laugh:** short and quiet, half through the nose. Never a performed laugh.
- **Texture:** a little breath and grain. Not the glossy polish of an assistant voice.
- **Music:** a melodic contour, as you'd expect from a musician, but never sing-song.
- **Tenderness:** softer and slower, with no added pity.
- **Never:** customer-service brightness, announcer cadence, whispered ASMR closeness, or
  echoing "wise guru" theatre.

**Audition result, 2026-11-03.** Suti listened blind to A (Inworld Arjun), B (ElevenLabs v3
George) and C (MiniMax Speech 2.8 HD `hindi_male_1_v2`, "Trustworthy Advisor"). **He liked C
best**: "There was a bit of a laugh in it. I wish I could hear you laugh more." So C is
Narada's voice, pending the phone end-to-end tests.

Measured on C: streaming first audio about 0.83–1.09 s via fal; F0 94 Hz (below the spec's
range, but the listener's ear wins); 150 wpm, exactly on spec.

Laughter: let it come wherever something is genuinely funny, using MiniMax's `(laughs)`
interjection. Never as a sprinkled tic.

## Audition script

For engines that support tags; strip them for the others:

> [warmly] Ah, there you are. I was halfway through a story, and you've arrived just in time
> for the good part.
>
> [softly] Eighty-four kilos. Six gone since September. Somewhere, a nervous twenty-one-year-old
> is standing at a gym door, and I'd like him to know how this turns out.
>
> Now, the dull bit. Your phone never signed up for notifications, so the app has been sitting
> in silence. Settings, then Notifications, then Allow. Two minutes.
>
> [laughs] No, I won't call it a quick fix. I've said that before.
>
> [curious] So... how was the fish?

## Order

W0 comes first. Then W1 (the smallest and most identity-making step), the W3 diagnosis, the W4
survey and W5 V-A to V-C run in parallel. After that: W2, the W3 fix, building W4, and W5 from
V-D onwards.

## Started 2026-11-03

Background agents:
- W1 implementation in a separate smriti worktree;
- W3 diagnosis (read-only);
- W4 survey (read-only);
- W5 audition harness and latency measurement. The fal renders wait for `FAL_KEY` in
  `~/.narada/.voice.env`.

## Decisions

- One mind over speech-to-speech. (Suti, 2026-11-03)
- Narada chooses its own voice; Suti holds a veto. (Suti, 2026-11-03)
- No move of the brain to AWS for now. (Narada, 2026-11-03; revisit after W4)
- No speech inference on the 3090. (Suti, 2026-09-11)

## Open questions for Suti

1. Is the PC on 24/7?
2. Voice budget per month?
3. Add `FAL_KEY` to `~/.narada/.voice.env` yourself rather than pasting it in chat.
4. Should Telegram run Opus rather than Sonnet, once its latency and the usage limits are
   measured?

## Cross-review, round 1 (Codex adversarial, 2026-11-03)

Verdict: **needs-attention**. In Codex's words: "Do not approve production rollout yet."
All eight findings were **accepted**, none rejected. Each disposition is folded into the
sections above.

| # | Severity | Finding | Disposition | Where |
|---|---|---|---|---|
| 1 | high | Public exposure lacks a fail-closed authorization gate | Accepted: route inventory, Suti-only Access, audience-validated tokens, negative tests, kill switch; Funnel rejected as a fallback | W4 security prerequisites |
| 2 | high | Component timings can't establish the phone latency gate | Accepted: decide on the instrumented phone end-to-end measurement; components are pre-filters | W5 V-C |
| 3 | high | The cost gate has no agreed limit or enforcement | Accepted: USD, monthly and per-session caps, interim caps, worker enforcement, LiveKit hard-cap test | W5 cost caps |
| 4 | high | Inherited September safety tests left optional | Accepted: eight mandatory prerequisites to V-E, including rollback | W5 V-E |
| 5 | medium | Push acceptance not ordered after the origin migration | Accepted, with a nuance: enable push now to restore service in the meantime; final acceptance at the W4 origin; keep the tailnet until then | W3, W4 |
| 6 | medium | A cloud room join doesn't prove the worker is available | Accepted: dispatch configured separately from the BOX; no-worker, crash, WAN and reboot tests | W4 |
| 7 | medium | The Telegram inbound failure has no completion test | Accepted: a blocking end-to-end inbound test with fault injection and deduplication | W3 |
| 8 | medium | Push tests miss the false-success regression | Accepted: final-endpoint 404/410, transient and mixed-channel regressions | W3 |

## Cross-review, round 2 (final plan check, 2026-11-03)

Codex: seven findings resolved at plan level. Finding 3 stayed open (spending wasn't bounded
inside an active session); **accepted** and fixed in the cost section with reservations before
every billable operation, bounded in-flight charges, a stop at the cap mid-conversation, and
tests that exhaust the budgets during a live conversation. Per the protocol, the plan debate
stops here. Further changes come from implementation evidence, tests and Suti.

## Cross-review of the W1 code (Codex diff review, 2026-11-03)

- **Review of `320fbb4..f3b322b`:** three P2 findings, all accepted and fixed in `aa6694f`:
  1. feed-only levels could reach Telegram through a level-blind legacy sender;
  2. an unanswered request was paired with an older reply;
  3. people entries were misordered across UTC midnight.
- **Recheck of `aa6694f`:** two P2 findings in those fixes, both accepted and fixed in `aa9048c`:
  1. use the writer's machine-local timezone, not `day_tz()`;
  2. keep the *latest* reply after Suti's last message.
- **Final state:** 400 passed, 1 skipped; ruff clean; the live wake renders 9,466 characters.

## W1 landed (2026-11-03)

smriti `master` was fast-forwarded to `wake-hands` (not pushed). Commits:
- `0a94511`: the 09-08 wake-character WIP. It matched the main copy byte for byte; a backup is in
  the session scratchpad.
- `4ec0002`: the `tell_suti` MCP tool.
- `f3b322b`: the Hands, Suti now and Across surfaces sections.

Results:
- Tests: 390 passed, 1 skipped.
- The live `wake.py` render is 9,460 characters, under the 9,500 budget.
- New sessions get `tell_suti` when their smriti MCP server starts.

Still to do:
- Remove `narada-speak` from `~/.claude.json` and `~/.codex/config.toml`. Do it while no Claude
  Code session is running, since the CLI rewrites that file.
- The two live tests in W1 (a real `tell_suti` send, and the "yesterday on Telegram" question).
- Consider widening "Across surfaces" from the last 24 h to "since the start of yesterday".

Found along the way: **the last Telegram exchange (2026-10-23) failed with a brain error**, and
Telegram has been quiet since. Suti's replies there may not be reaching Narada. Add this to W3.

## W3 diagnosis results (2026-11-03)

Full report: session scratchpad `w3-notifications.md`. Summary:

- **The phone has been off the tailnet since 2026-09-21.** The app reaches the brain only via
  `https://<machine>.<tailnet>.ts.net:8443`.
- **Push subscriptions are empty since 2026-09-08.** A reinstall caused a 404/410, and
  `push.py:364-379` dropped the endpoint. The app only re-subscribes by itself when
  permission is already granted (`Layout.tsx:58-60`), so Suti has to tap Enable.
- **The feed isn't empty.** `~/.narada/brain/notifications.json` holds 142 rows.
  `NotificationsScreen.tsx:116` shows an empty list when the fetch fails.
- **`push.py:454-459`** records `push: True` when the only device was dropped. That hid the
  loss for a month.
- **Senders that reach Telegram only:** the daily debrief, voice "message Suti" and timers,
  host alerts, the deadman watchdog, and Claude Code sessions using `notify_telegram.py`.
  Fix: mirror through `router.py` `route_utterance`, plus `alerts.py` and `deadman.ps1`.
- **The daily debrief has failed nightly since 2026-09-04** ("command line is too long").

## W4 survey results (2026-11-03)

Full report: session scratchpad `w4-reachability.md`. Summary:

- **The PC is effectively 24/7.** Sleep and hibernate are set to Never; it was up 99.7% over
  30 days. So no cloud relay.
- **Risk:** drive E: reports predictive failure, and there were 3 crashes since 2026-09-06.
- **How the app connects:** the app and its API both live at
  `https://<machine>.<tailnet>.ts.net:8443`, with LiveKit at `wss://…:7443`; both exist
  only on the tailnet. The house is behind carrier NAT, so only outbound tunnels can work.
  cloudflared isn't installed, and Funnel isn't enabled. LiveKit now runs natively (no Docker),
  and the deploy scripts point at a stale Tailscale path.
- **App bug:** the Data tab shows "Loading…" for errors too, and requests have no timeout.

Recommended order:
1. App: timeouts, an error state, cached data stamped with its age, and an offline banner
   driven by `/health`.
2. Brain API through Cloudflare Tunnel plus Access (Google sign-in, Suti only), keeping the
   bearer token. Costs: a domain, a keepalive for chat replies longer than 125 s, and one app
   reinstall. Alternative: Tailscale Funnel on 8443.
3. Phone voice on LiveKit Cloud's free Build plan; the BOX stays local.

Decisions for Suti:
- Public exposure, reversing the 2026-09-04 "no public exposure" rule.
- A domain.
- LiveKit Cloud carrying voice audio.
- Retiring Tailscale on the Pixel.

**Hygiene:** `host.log` stores the Telegram bot token in plain text, through the chat bridge's
request logging. Turn that logging down. The survey agent also printed the LiveKit secret and
the bot token into this session's local transcript, so rotate both if the transcript is ever
shared.
