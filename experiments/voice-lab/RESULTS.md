# Voice lab results

## Gemini Live talker, November 3, 2026: built, paid smoke not yet run

`~/.narada/.keys.env` has no `GOOGLE_API_KEY` yet, so **nothing has been
spent on Google** and there are no latency or cost measurements.
`smoke_gemini.py` exits with the setup steps (see README) and spends nothing.

What was verified without spending:

- 32 unit tests pass (`tests/`), on livekit-agents 1.8.10 and
  livekit-plugins-google 1.8.10. They cover the cost guard (cap exhausted
  mid-session stops forwarding audio; first turn refused before connecting;
  trial total; late usage still charged), the idle timeout (including
  holding it while a reply is still playing), the transcript states
  (played, interrupted, unplayed; only heard speech in `heard`), the
  latency meter, pricing, and the plugin hook signatures. Disabling the
  audio gate makes four of them fail.
- Wiring check against the local LiveKit server with a deliberately
  invalid key: `/health` offered `gemini`; a call opened with a reserved
  first-turn ceiling of $0.0076. Google rejected the key (WebSocket 1007,
  "API key not valid"), the call closed cleanly and wrote its transcript
  file with $0 spent. A $0.001 session cap was refused with HTTP 402
  before any connection.

Open questions the first paid run answers: Gemini's per-turn usage
(whether the whole context is re-billed each turn), real end-of-speech to
first-audio latency from Brisbane, whether `max_output_tokens` bounds
audio replies, and whether Umbriel can laugh the way the spec asks.

## September 11, 2026

### GPT-Live comparison, now available

The listening page at **http://localhost:8899** now offers GPT-Live and both
Realtime mini settings. This uses an isolated LiveKit Agents 1.8.1 / OpenAI
2.54.0 environment; the production environment and workers were not changed.
GPT-Live uses `marin` and a short delegation prompt. Mini uses `cedar` and its
existing lab prompt. This compares usable stacks, not the model alone.

Identical cached user audio requested a slow lookup, then asked two plus two
while it ran. Both calls returned nonzero audio, answered before completion,
announced mango seven afterward, and closed with HTTP 200.

| Observed measure | GPT-Live-1 | Realtime 2.1 mini, semantic low |
|---|---:|---:|
| Test wall duration, including setup/cleanup | 48.92 s | 47.81 s |
| Lookup start / completion | 6.000 / 36.000 s | 5.656 / 35.656 s |
| Arithmetic reply transcript completed | 17.969 s | 17.609 s |
| Result reply transcript completed | 40.891 s | 37.984 s |
| Provider voice cost from recorded usage | US$0.038333 | US$0.008990 |
| Separate input transcription | Included | US$0.000268 |
| Total modeled API cost | **US$0.038333** | **US$0.009257** |

In this one short scenario, Live cost **4.14 times as much**. These are
calculations from provider usage, not a billing-dashboard reconciliation or
a projection for production calls. Mini's cost includes prompt/history,
cache, output reasoning and input transcription. Live's final close reported
46 billable seconds; intermediate cumulative snapshots were not added.
Neither run invoked a backend model. Cached synthetic TTS fixtures, hosting,
taxes and any production brain usage are outside this comparison.

The Live acknowledgment was one short reply: "Sure, starting that now. I'll
keep an eye on it." Mini's longer acknowledgment was interrupted by the
second question. This is one observation, not a human preference result.
Failure, cancellation, barge-in, echo and long calls have **not** been tested
on GPT-Live by this short comparison. The earlier full checks below apply
only to mini. No speech inference ran on the 3090.

- [Live report](output/compare-live-c984b925386b47ddb669faaaddaf9e07.json)
  and [audio](output/compare-live-c984b925386b47ddb669faaaddaf9e07.wav).
- [Mini report](output/compare-semantic-9271ef6fdc4b41b4a6f92d42dc63b48c.json)
  and [audio](output/compare-semantic-9271ef6fdc4b41b4a6f92d42dc63b48c.wav).
- `cost.py` reproduces the usage calculations from either report.

[Official pricing](https://developers.openai.com/api/docs/pricing), checked
September 11: Live is US$0.05 per session minute; mini audio input/cached/output
is US$10 / $0.30 / $20 per million tokens, text $0.60 / $0.06 / $2.40.
Mini Transcribe input/output is $1.25 / $5 per million tokens.
[Live's session clock](https://developers.openai.com/api/docs/guides/voice-latency-cost)
includes silence and time waiting for tools. A connected five-minute Live
call costs $0.25 and an hour $3, plus any backend model/tool charges. Muting
does not end billing. The lab's dummy backend has no separate model charge.

An initial mini startup in the new environment failed because the metering
subclass did not forward the new `turn_detection_disabled` keyword. That
was corrected before the accepted mini run. Python/JavaScript syntax and
HTTP page/asset/health checks passed; browser microphone interaction awaits
Suti's listening test.

## Earlier Realtime-only findings

**The OSS media and in-session tool mechanics work in both configurations.**
Conversational polish remains unfinished: both runs repeated lookup
acknowledgments, and the mini model sometimes narrated the instructions
(“I’ll acknowledge that…”) instead of simply speaking naturally.

Tested on Windows with LiveKit Agents 1.6.8 and the existing local LiveKit
server. Speech ran on hosted `gpt-realtime-2.1-mini`, voice `cedar`. No speech
model was loaded on the RTX 3090. Production workers were not changed or
restarted. The lab contains no continuity or initiative integration.

## Accepted audio runs

Synthetic user speech was published over a real WebRTC microphone track;
the agent's returned audio was captured. These are engineering checks, not
human listening scores or statistically meaningful performance benchmarks.

| Check | Semantic VAD, low eagerness | Server VAD, 700 ms silence |
|---|---|---|
| Actual audio round trip and user transcription | Pass | Pass |
| Answer a second question before the 30-second lookup completes | Pass | Pass |
| Speak the completed lookup result | Pass | Pass |
| Finish playback of an affirmative tool-failure report | Pass | Pass |
| Explicitly cancel a running lookup | Pass | Pass |
| Interrupt a story and follow the new instruction | Pass | Pass |
| Close the test session | HTTP 200 | HTTP 200 |

Accepted semantic run: `fe6d50671c084602ae5e5e42b9c17b60`, 118.64 seconds.
Accepted baseline run: `5870aa535be041109fbfd9d9151e3889`, 109.75 seconds.

- [Semantic report](output/smoke-semantic-fe6d50671c084602ae5e5e42b9c17b60.json)
  and [returned audio](output/smoke-semantic-fe6d50671c084602ae5e5e42b9c17b60.wav).
- [Baseline report](output/smoke-server-5870aa535be041109fbfd9d9151e3889.json)
  and [returned audio](output/smoke-server-5870aa535be041109fbfd9d9151e3889.wav).

Both answered “4” while the lookup was pending, then later said “mango seven.”
The semantic final failure report was “The lookup failed, and no result is
available.” The baseline report was “The lookup failed, so no result is
available.” Cancellation produced an actual `lookup_cancelled` event; the
cancelled call did not subsequently complete in the recorded scenario.

Recorded tool completion to **end of spoken result** was 3.89 seconds in the
semantic run and 2.64 seconds in the baseline run. These are not first-audio
latencies, and differences in wording/playback make them unsuitable for
declaring a winning endpoint strategy.

## What needs improving

1. **Duplicate acknowledgment.** Both configurations often produced two
   consecutive “lookup started” statements. Shortening the prompt did not
   reliably remove this. Inspect the model's pre-tool speech and LiveKit's
   follow-up after `ctx.update()` before adding more prompting or filler.
2. **Instruction narration.** The model sometimes said what it would
   acknowledge. That is poor conversational output despite correct tools.
3. **Patient endpointing.** Semantic low eagerness can wait noticeably after
   a spoken interruption. Test Suti's actual hesitations before choosing it
   as the default. Stopping speech and starting the next response are separate
   measurements.
4. **Natural backchannels and echo are untested.** Synthetic input does not
   reproduce a person saying “mm-hm,” speaker leakage, room noise, or a BOX
   microphone. No canned backchannel loop was added.

The next voice-only step is a paired listening test at
**http://localhost:8898**, then an isolated correction/comparison for the
weakest conversational behavior. A different hosted model can be compared
without moving speech inference onto the 3090. Pipecat remains a useful OSS
framework comparison if the observed limitation is in orchestration.

## Test-development corrections

An initial startup failed because the plugin expects typed OpenAI turn
detection configuration objects; that adapter configuration was corrected.

Early failure assertions accepted pending acknowledgments containing the
word “failure,” or quoting what the agent would say *if* it failed. The
harness then spoke over the actual error report. Those reports were marked
superseded and excluded from the accepted results above.

The final check waits for `tool_reply_updated(status="completed")` for the
failed call's final update, then checks the last assistant transcript before
that playback event. The accepted semantic recording was revalidated against
this strengthened check; the accepted baseline ran it live. One intermediate
semantic run was also excluded because an over-strict text check rejected a
real error report and stopped before later scenarios.

The browser page and client asset returned HTTP 200; Python and browser
JavaScript syntax checks passed. Foreign-origin call creation returned 403.
Browser microphone interaction and subjective listening remain for Suti.

This experiment demonstrates concurrency within a live call. It makes no
claim about durable work after a process crash, personal continuity, initiative,
long-call stability, phone/BOX performance, or ChatGPT-equivalent voice quality.
