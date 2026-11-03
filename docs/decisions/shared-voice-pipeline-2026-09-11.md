# Shared GPT-Live voice pipeline

Suti accepted GPT-Live's quality and cost on September 11 and asked to adopt
it, retain a Settings model selector, share one pipeline between app and BOX
S3, and make the delivery more expressive and recognizably Narada.

## What runs

Both `voice.worker` (BOX admission and display) and `voice.phone_worker`
(authenticated app dispatch) now use `voice.pipeline.VoicePipeline`.
Both resolve the model from the same saved `brain/voice-prefs.json` on the
next call. The live saved selection is `gpt-live-1`; Realtime mini and the
existing Realtime alternatives remain selectable in app Settings.

The device adapters remain separate: hardware tap/wake, microphone defaults,
display captions and music ownership differ from browser auth, WebRTC and
call controls. Model construction, delivery, delegation and provider usage
are shared. The automatic BOX worker declines both `akhada-phone-*` and
`narada-phone-*` rooms, avoiding a second agent in the app's call.

GPT-Live uses `marin`, native conversation timing, and stock LiveKit Responses
delegation to `gpt-5.6-luna`. That backend selects from the existing, tier-filtered
`build_voice_tools` registry; it does not gain new permissions or replace the
deeper Narada escalation path. Admission, proposal gates, transcript redaction,
silence/session limits and the shared minutes budget remain in place.
The backend receives the existing admitted context pack. The voice model gets
a shorter character/delivery/delegation prompt and asks the backend for personal
context. This is different from duplicating the whole pack into the speaking
model; test conversational continuity before expanding its direct context.

Narada's delivery calls for bright curiosity, affectionate dry wit, quiet care,
and varied rhythm in stories. It avoids compulsory laughs, catchphrases and
spoken stage directions. Indian English remains an unforced accent instruction.
These steer delivery; neither accent nor a particular emotional performance is
guaranteed. See [GPT-Live prompting](https://developers.openai.com/api/docs/guides/live-prompting).

## Controls and cost

Live does not use Realtime VAD/noise parameters; Settings disables those while
Live is selected. Native backchannels replace the BOX's prerecorded listening
clips, and the Realtime reply watchdog does not force Live turns.
Live TV mode asks it to ignore television; it is not Realtime's hard no-barge-in
gate. A tap still ends the call.

Live is US$0.05 per connected minute, including silence, plus backend usage.
The backend is now real (unlike the earlier lab's dummy client backend), so
token charges apply when it delegates. Usage events log separately for Live,
the backend, Realtime and transcription; final Live duration is logged after
close. The former audio-only flat-rate estimate was removed because it was
wrong for both mini token modalities and Live sessions.

## Runtime and deployment

Hosted inference stays off the 3090. `.venv-voice` has LiveKit Agents/OpenAI
plugin 1.8.1, SDK 2.54.0 and NumPy 1.26.4, with system packages visible for
existing prana/deha/akhada dependencies. The worker mains relay to that
interpreter while the supervisor retains the parent and Windows process job.
`NARADA_VOICE_PYTHON` can set an explicit interpreter. A packaged install
without that local environment uses the declared voice-extra dependencies.
No base Python or Hermes package set was upgraded.

Create it from the repo using an explicit base interpreter:

```powershell
uv venv --python C:/ProgramData/anaconda3/python.exe --system-site-packages .venv-voice
uv pip install --python .venv-voice/Scripts/python.exe "livekit-agents[openai]~=1.8.1" "numpy>=1.26,<2"
```

The new PWA bundle was published to `%LOCALAPPDATA%/narada/pwa`; its previous
deployment is backed up beside it as `pwa-before-gpt-live-20260911`. The brain
and two voice components were restarted while no call/brain turn was active;
the rest of the supervisor remained running. Roll back voice selection to
Realtime mini in Settings; this retains the shared pipeline. The older 8898
lab and separate 8899 GPT-Live listening lab remain isolated experiments.

## Evidence and limits

- Shared-pipeline real-audio runs: Live `37b1372e3be242c9a382c433448a84ac`
  and mini `6f043ef2045a4ee88fe2605b2aa01e3b`. Both answered during the
  30-second dummy lookup and returned mango seven afterward. The shared lab
  mini run used the pipeline's server-VAD default (its report's `semantic`
  label is the lab selector, not the configured production VAD).
- BOX emulator: one normal tap/open/greeting/stop/recycle cycle passed;
  54 received greeting frames. This exercises the real BOX adapter with a
  simulated microphone, not the physical room's echo or TV.
- Deployed phone route: synthetic call `narada-phone-1789088029809-508fcc`
  received 25.35 seconds of audio; running worker logged GPT-Live/marin and
  completed backend self-description. The fixture is automated test speech,
  not Suti. Provider final usage was 24 seconds ($0.02 voice plus backend).
- Settings served GPT-Live selected with `scope=app_and_box`; all three
  production health endpoints returned 200 and the BOX room had device+agent.
- Targeted Python admission/budget/context/settings/transcript checks passed;
  frontend voice/Settings tests passed (31), and production build passed.

Human quality feedback was positive on the earlier isolated Live lab. Physical
BOX echo/TV, expressive delivery preference, Live long-call stability and full
tool failure/cancellation behavior need field listening. Voice alone does not
provide durable cognition, initiative or recovery of work after process death.

## Tap startup repair

The physical BOX tap at 10:59 reached admission but crashed on the first
pipeline import: LiveKit requires plugin registration on the main thread.
A cleanup after the earlier audio tests had removed the worker-level OpenAI
import, leaving registration until the job thread. Both workers now explicitly
import the plugin at startup. Fresh-process tests import each worker, then
import the pipeline inside a job thread; all eight pipeline tests pass.

The first emulator retry timed out before admission; a retry with worker logs
captured passed at 11:05 (1.50 seconds to nonce, 1.75 seconds to session open,
50 greeting frames, 5.28 seconds to recycle). Both production voice workers
were restarted with the repair, leaving the host and brain running. Physical
tap/audio confirmation remains a user check.

Suti subsequently confirmed the physical BOX tap works. He asked why its
voice was feminine: the selected voice remained Marin from the successful
trial. Settings now exposes Speaking voice separately from Voice model,
with `live_voice` and `realtime_voice` saved independently in shared prefs.
Both surfaces use `selected_voice`; changing model families preserves each
choice. The API validates built-in choices against their family. Existing
environment overrides remain defaults when no voice is saved.

Voice labels follow the current official GPT-Live session guide. Vesper
was exercised through the BOX emulator with received greeting audio. API,
pipeline and settings tests cover persistence, switching, invalid choices
and failed-save rollback; the production app build passes. New voices apply
on the next call. Custom voice enrollment requires eligible account access
and the actor's consent/sample recordings; no enrollment is implemented here.

The shared pipeline is audio, not daily scene delivery. The Sep11 monsoon
pack was produced for phone preview; current BOX `main/scene.c` still embeds
the grove artwork. Its temporary JPEG picture overlay hides on tap and is
not a daily pack renderer. BOX pack delivery remains separate firmware work.
