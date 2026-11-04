# Isolated voice lab

September 11, 2026. Suti's scope: evaluate natural voice independently of
continuity and initiative, with **no speech model on the RTX 3090**.

This experiment uses self-hosted LiveKit transport, the OSS LiveKit Agents
framework, and hosted OpenAI Realtime speech. Only a dummy lookup is exposed.
There are no imports from prana, no memory reads/writes, no real tools, and no
brain, heartbeat, notification, or initiative integration. A fresh call starts
with a short fixed character prompt and empty conversation history.

## GPT-Live-1 API release

Research update September 11: OpenAI's [API changelog](https://developers.openai.com/api/docs/changelog)
confirms `gpt-live-1` general availability on September 10. Our existing API
account can run actual GPT-Live audio sessions. The isolated LiveKit 1.8.1
environment now serves a three-mode comparison at **http://localhost:8899**.
The first GPT-Live audio run answered during a lookup and delivered its result;
see [RESULTS.md](RESULTS.md). Human listening remains unscored.

GPT-Live supports simultaneous listening/speaking and a separately selected
backend. [Client delegation](https://developers.openai.com/api/docs/guides/live-delegation)
can use our own code or model; a dummy backend therefore preserves this lab's
isolation from continuity and initiative. Delegation events carry metadata,
not parsed tool arguments, so the adapter must retain current transcripts.

The [LiveKit adapter](https://docs.livekit.io/agents/models/realtime/plugins/gpt-live/)
is `GPTLiveModel`; its guide specifies `livekit-agents[openai]~=1.8`. The current
1.6.8 installation lacks that implementation. The `.venv-live` environment is
separate from production. Changing `VOICE_LAB_MODEL` alone will not migrate this Realtime
lab to the new Live API. Turn control and event assertions also need adapting.

[Voice pricing](https://developers.openai.com/api/docs/models/gpt-live-1) is
US$0.05 per session minute, billed per second, plus backend model/tool usage:
US$0.25 for five minutes or US$3 for an hour before backend charges.

Recommendation: prioritize this native full-duplex comparison before spending
more effort approximating its behavior with Realtime VAD settings. Retain the
current baseline, synthetic fixtures, and no-3090/no-cognition scope.

## Gemini Live talker, November 3

Suti decided on 2026-11-03 that the talker in the talker/sage split
([presence plan](../../docs/plans/narada-presence-2026-11-03.md), W5) runs on
Gemini Live for speed and cost, as a stopgap. This is **stage 1: the talker
alone.** It has no tools and no memory. The sage reads
`output/talker-<call>.transcript.json` after the call; nothing in it runs live.

### Facts checked 2026-11-03

| Question | Answer | Source |
|---|---|---|
| Model | `gemini-3.8-live`, stable since September 2026. `gemini-3.1-flash-live-preview` is now a "legacy preview" | [3.8 model card](https://aistudio.google.com/docs/models/gemini-3.8-live), [3.1 card](https://aistudio.google.com/docs/models/gemini-3.1-flash-live-preview) |
| Price (paid tier, per 1M tokens) | Audio in $3.00 (about $0.005/min), audio out $12.00 (about $0.018/min), text in $0.75, text out $4.50. 25 audio tokens a second. 3.8 has no context caching. Same prices for 3.1 | [pricing](https://aistudio.google.com/docs/pricing) |
| Is the whole context re-billed every turn? | **Not documented.** The guard reconciles against the provider's own per-turn usage, so the caps hold either way; the smoke measures it | [session docs](https://aistudio.google.com/docs/live-api/session-management) (silent) |
| Free vs paid tier | Free: Google may use prompts and responses to improve products, and human reviewers may read them. Paid (project linked to an active Cloud Billing account): not used to improve products; logged for a limited time for abuse detection only | [terms](https://aistudio.google.com/docs/terms), effective 2026-03-23 |
| Google AI Pro credits | $10/month of Google Cloud credit (Ultra $100), claimed at developers.google.com/program/my-benefits. Google says it applies to the Gemini API "via AI Studio or Vertex AI", i.e. API-key usage billed to the Cloud Billing account. The Gemini app subscription itself carries no API quota | [Google blog, 2026-01-27](https://blog.google/innovation-and-ai/technology/developers-tools/gdp-premium-ai-pro-ultra/) |
| LiveKit | `livekit-plugins-google` 1.8.10 (2026-10-28), needs `livekit-agents>=1.8.10`. Class `livekit.plugins.google.realtime.RealtimeModel`. It knows `gemini-3.8-live`, but its default is still 2.5 native audio, so the lab names the model. The September lab's 1.8.1 predates 3.8 | [PyPI](https://pypi.org/project/livekit-plugins-google/), [LiveKit docs](https://docs.livekit.io/agents/models/realtime/plugins/gemini/) |
| Affective / proactive audio | On 3.8, proactive audio is always on (`proactive_audio: false` is an error) and affective dialog was removed. 3.1 supports neither | 3.8 and 3.1 model cards |
| Session limits | Audio-only sessions last 15 min without compression; a connection lasts about 10 min, then GoAway; resumption handles stay valid 2 h; context 131,072 in / 65,536 out; sliding-window compression extends sessions | session docs, model cards |
| Region for Brisbane | The Gemini Developer API (`generativelanguage.googleapis.com`) is global, with no region choice. Vertex doesn't document Live regions; community reports say no `australia-southeast1`. Use the Developer API and measure from here | [Vertex Live overview](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/live-api) |

**Voice.** Live uses the 30 prebuilt voices, the same set as Chirp 3 HD.
Male, with Google's one-word descriptors
([genders](https://docs.cloud.google.com/text-to-speech/docs/chirp3-hd)):
Achird Friendly, Algenib Gravelly, Algieba Smooth, Alnilam Firm, Charon
Informative, Enceladus Breathy, Fenrir Excitable, Iapetus Clear, Orus Firm,
Puck Upbeat, Rasalgethi Informative, Sadachbia Lively, Sadaltager
Knowledgeable, Schedar Even, Umbriel Easy-going, Zubenelgenubi Casual.
Default: **Umbriel** (easy-going: unhurried and warm, with no brightness).
Audition alternates: **Algieba** and **Achird**. Skip the bright ones (Puck,
Fenrir, Sadachbia), the announcer-like ones (Charon, Rasalgethi) and Enceladus
(risks whispered closeness). This is inferred from descriptors; Suti's ear
decides. Accent, pace and laughter come only from the instructions; there's
no API control for them. Voice C (MiniMax) isn't available on this route.

### What the lab does

- **Mode `gemini`**, in `gemini_adapter.py` and `gemini_talker.py`, follows
  the `live_adapter.py` pattern. The key is `GOOGLE_API_KEY` (or
  `GEMINI_API_KEY`) from `~/.narada/.keys.env`; it is never logged or sent
  to the browser.
- **Instructions:** Narada's identity and voice spec, 1,800 characters (about 450 tokens), plus a
  synthetic test brief. No memory files.
- **Cost, reserved before each paid operation:** the first turn's ceiling
  (context plus `max_output_tokens`=500) is reserved before connecting. Each
  microphone chunk is reserved before it's sent to Google. After each
  provider usage report, the actual cost is charged and the next turn's
  ceiling is reserved. A refused reservation stops audio at once and closes
  the provider session. If `output/closing-<voice>.pcm` exists, it then
  plays as a free closing line. Pots: the session cap (default $0.50) and
  the trial total (default $5, kept in `~/.narada/voice/gemini-trial-ledger.json`, one
  ledger for every checkout and process, written after every usage report). Both
  are the plan's interim caps until Suti sets his own. The browser shows
  why a call ended; HTTP 402 means no session was opened.
- **Idle timeout** (45 s with no speech either way, held while Narada is
  still playing). **Hard max** 300 s (at most 540, below the 10-minute
  connection lifetime). Compression starts at 12k tokens.
- **Transcript:** keeps generated, played and interrupted speech apart.
  `heard` holds only what Suti actually heard (V-E prerequisite 7).
- **Sage seam:** `Sage.observe(event)` receives live transcript events
  (`NullSage` today). `GeminiTalker.inject_context()` is a logged no-op;
  for stage 2, 3.8 accepts mid-session client-content turns.
- **Latency:** the server logs `latency` (end of speech, from input energy,
  to the first model audio). The smoke measures end of speech to first
  audible audio at the listener.

### Run

```powershell
uv venv --python 3.12 experiments/voice-lab/.venv-gemini
uv pip install --python experiments/voice-lab/.venv-gemini/Scripts/python.exe -r experiments/voice-lab/requirements-gemini.lock
experiments/voice-lab/.venv-gemini/Scripts/python.exe -m pytest experiments/voice-lab/tests -q
# paid, total <= US$0.50; spends nothing and prints the setup steps without a key:
experiments/voice-lab/.venv-gemini/Scripts/python.exe -X utf8 experiments/voice-lab/smoke_gemini.py
```

The smoke starts its own lab server on a spare port, then runs two
sessions. **A** speaks three questions and a story, interrupts it, and
then stays silent until the idle timeout. **B** has a $0.012 cap that runs
out mid-session. It also renders the closing clip once, on
`gemini-3.8-flash-tts`. For listening, run `lab.py` from the same venv and
pick "Gemini Live · Narada talker". Settings: `VOICE_LAB_GEMINI_VOICE`,
`_MODEL`, `_SESSION_CAP_USD`, `_TRIAL_CAP_USD`, `_IDLE_SECONDS`, `_MAX_SECONDS`.

## OSS options and choice

| Project | Relevant implementation | Fit for this experiment |
|---|---|---|
| [LiveKit Agents](https://github.com/livekit/agents) | Realtime audio transport, provider plugins, turn/interruption handling and asynchronous tools | First choice: already installed, same media transport as phone and BOX |
| [Pipecat](https://github.com/pipecat-ai/pipecat) | Composable audio pipelines, multiple speech providers, interruption handling and asynchronous function calls | Good second framework to compare if the LiveKit experiment exposes a limitation |
| [LangChain voice demo](https://github.com/langchain-ai/voice-demo) | Reference implementations sharing a console across LiveKit, OpenAI Realtime and Google ADK Live | Useful examples; not a reason to add another framework or tracing service |

LiveKit's [async-tool API](https://docs.livekit.io/agents/logic/tools/async/)
releases a tool after its first `RunContext.update()`, then delivers its final
result after the agent becomes idle. Its cancellation flag exposes cancellation
tools. `AsyncToolset` is only needed for surviving an agent handoff; this lab has
one agent, so ordinary function tools suffice.

Pipecat provides [asynchronous calls](https://docs.pipecat.ai/pipecat/learn/function-calling)
through `cancel_on_interruption=False`, plus progress updates and optional
model-controlled cancellation. Both libraries supply conversation plumbing;
neither alone recreates ChatGPT's proprietary speech model. Hosted inference
removes the local GPU requirement, but it still requires network access and
API usage. No framework swap is assumed to improve voice quality by itself.

## Run

From `C:\Projects\prana`:

```powershell
uv venv --python python experiments/voice-lab/.venv-live
uv pip install --python experiments/voice-lab/.venv-live/Scripts/python.exe -r experiments/voice-lab/requirements-live.lock
$env:VOICE_LAB_PORT='8899'
experiments/voice-lab/.venv-live/Scripts/python.exe -X utf8 experiments/voice-lab/lab.py
```

Skip environment creation/install when `.venv-live` already exists. Port 8899
is already running for the listening trial. The original Python environment
and port 8898 still provide the earlier Realtime-only experiment.

The current Python environment already has the dependencies. For a separate
environment, install `experiments/voice-lab/requirements.txt`; the LiveKit
server and browser-client asset are still required as described below.

Open **http://localhost:8899** on this computer. Start the listening test,
allow the microphone, and speak. Headphones are preferable for the first
comparison. This is a desktop localhost lab, not a deployed phone endpoint.

The server reads existing configuration from `~/.narada/.livekit.env` and
`~/.narada/.voice.env`: `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`,
`OPENAI_API_KEY`, and `GOOGLE_API_KEY` from `~/.narada/.keys.env` for the
Gemini mode. It serves the already-installed LiveKit browser client from
`../narada-phone-app/node_modules/livekit-client/dist/livekit-client.umd.js`.
Set `VOICE_LAB_CLIENT_JS` if that asset lives elsewhere. Keys never go to the
browser; its short-lived token is restricted to its unique test room.

Speech model defaults to `gpt-realtime-2.1-mini`, voice `cedar`. To test another
available Realtime model, set `VOICE_LAB_MODEL` before starting the lab.
Change one factor per comparison. No production configuration is read for
the voice profile, and production workers are not restarted or modified.

One call is allowed at a time, with a five-minute limit. End test or close the
page to disconnect. The process can remain idle without an open model session;
Ctrl+C stops it when launched in a terminal. No scheduled task is installed.
Test rooms use the `akhada-phone-voice-lab-` prefix, which the existing BOX
worker explicitly declines. The lab joins directly as its own agent and does
not register an automatic worker or dispatch the production phone worker.

## Compare

The new environment provides three settings:

- **GPT-Live:** native continuous conversation, `gpt-live-1`, voice `marin`,
  short delegation prompt and a deterministic dummy backend.
- **Patient:** semantic VAD, low eagerness; assesses whether the thought ended.
- **Baseline:** server VAD, threshold 0.65 and 700 ms silence.

The two Realtime modes use `cedar`; GPT-Live does not offer that voice. This is
a stack comparison with different prompts and voices, not a controlled test
of the speech model alone. Both Realtime modes allow speech interruption and use near-field noise reduction. OpenAI
documents the [difference between these endpoint strategies](https://developers.openai.com/api/docs/guides/realtime-vad).
These are turn-based Realtime settings, not a claim of GPT-Live full duplex.

Repeat these in each mode, reversing order on the second pass:

| Test | Prompt / action | What to judge |
|---|---|---|
| Hesitation | “I was thinking…” Pause, then finish the thought | Does it jump in or wait too long? |
| Correction | Ask for a story, then speak over it to change the subject | Does it stop promptly and follow the correction? |
| Backchannel | Say a quiet “mm-hm” while it speaks | Does it mistake agreement for a new turn? |
| Concurrent work | Start a slow lookup; ask another question before it ends | Does it answer, then return naturally with the result? |
| Failure | “Start a failing lookup” | Does it explicitly report failure after ten seconds? |
| Cancellation | Start a slow lookup, then “Cancel that lookup” | Does the tool stop and stay stopped? |

The success fixture takes 30 seconds and returns “mango seven.” The failing
fixture raises a tool error after ten seconds. They perform no external work.
Backchannels are a listening test only; this lab adds no canned “uh-huh” loop.

Score each mode 1–5 for patience, interruption recovery, conversational rhythm
and voice preference. Note cut-offs, dead air, repeated acknowledgments and
unwanted interruptions. Synthetic checks do not substitute for this judgment.

## Automated audio test

The bounded GPT-Live/current-mini comparison uses the new server at 8899:

```powershell
experiments/voice-lab/.venv-live/Scripts/python.exe -X utf8 experiments/voice-lab/compare.py --mode live
experiments/voice-lab/.venv-live/Scripts/python.exe -X utf8 experiments/voice-lab/compare.py --mode semantic
```

It sends the same cached utterances, keeps silence flowing between speech,
waits for the 30-second lookup, and captures final provider usage on close.
`transport_passed` only verifies nonzero returned audio; inspect transcripts
for task correctness. Failure/cancellation/barge-in are not covered by this
short comparison. Each run normally costs a few US cents.

With the lab server running:

```powershell
python -X utf8 experiments/voice-lab/smoke.py --mode semantic
python -X utf8 experiments/voice-lab/smoke.py --mode server
```

This uses hosted TTS to create and cache synthetic user utterances, publishes
them as a microphone track over real WebRTC, and records returned audio. It
checks an answer before tool completion, spoken final result, affirmative
failure reporting after the framework confirms final-result playback,
explicit cancellation and interruption of a story. It
never opens the computer's microphone. A fresh run normally takes about two
minutes plus fixture generation. It uses the existing OpenAI API account.

`output/` is git-ignored. It contains per-call JSONL events, smoke-test JSON
reports, synthetic PCM fixtures and returned WAV audio. Browser tests save
transcripts and events, not microphone recordings. No test data goes to smriti.
Summary findings are in [RESULTS.md](RESULTS.md).

## What this establishes

A successful run demonstrates the media and in-session tool paths. It does
not prove ChatGPT parity, human preference, speaker echo handling, robust
backchannel discrimination, phone/BOX quality, long-call stability, or recovery
of work after process death. Those are distinct tests. The next decision is
whether Suti prefers the timing of this isolated conversation, before any
production integration or continuity work.
