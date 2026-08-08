# Voice-as-Narada, body revival, and service hardening

**Date:** 2026-08-08
**Status:** DRAFT v2 — cross-reviewed (Codex, 6 findings, all accepted; see §Cross-review). Track A hardened; Track C downgraded to PARTIAL.
**Follows:** `embodiment-rebirth-2026-08-06.md` (Phases 0–2 complete; voice
loop proven end-to-end 2026-08-07).

Where we are: the voice loop works end-to-end — synthetic speech in →
LiveKit → worker → `gpt-realtime-2.1-mini` → real tool call → spoken
answer out, verified with live machine data. Measured round-trip ~1.8 s
to first audio (~500 ms of that is tunable VAD). What's left splits into
three tracks.

---

## Track A — Make the voice actually Narada (deferred from 2026-08-07)

Today the voice model gets a 6-line persona prompt and 4 session tools.
It has **no** smriti memory, no identity, no journal, and no path to the
memory-having layer for a general question. It's a fast shell, not
Narada. Three additions, in increasing cost/depth:

1. **Identity/context digest in the session prompt** (cheap, static).
   At session start, build the instructions from: a trimmed identity
   essence (who Narada is, voice/values), `wake-context.md`, and a short
   recent-journal summary. Gives "knows who it is + recent goings-on"
   without per-turn recall. Watch prompt bloat; cache it per-day.
2. **A voice-safe memory *projection*, NOT general `smriti_read`**
   (cross-review #1). Read-only ≠ safe: recall is a *disclosure*
   boundary — anyone in earshot could make Narada speak private
   memories aloud, and everything recalled is sent to OpenAI. Build a
   scoped recall that **excludes `people/`, `journal/`, `identity`,
   credentials, and other private branches by default**; only a
   curated allowlist of shareable branches is voice-recallable;
   sensitive recall requires an authenticated confirmation channel
   (Telegram tap), never a spoken ask. Minimise identity context sent
   to the provider. Adversarial tests must prove prohibited memories
   cannot be retrieved or spoken.
3. **A sandboxed, answer-only `escalate_to_narada`** (cross-review #2).
   Routing spoken (untrusted) input to `claude -p` with full tools is
   an injection surface — a crafted question could induce reads/writes/
   commands, bypassing the voice tier's code-enforced mutation
   boundary. The escalation runner must: disable mutation-capable
   tools and inherited project MCP/hooks, expose only explicitly
   scoped read tools, pass the speech as clearly delimited *data* (not
   instructions), run in an isolated cwd, and enforce timeout +
   bounded concurrency + cancellation. Every mutation still goes
   through the existing proposal/capability path. Higher latency is
   the honest depth-vs-speed tradeoff; the voice says "let me think
   about that properly" while it runs.

**Recommended build order:** 1 + 2 first (identity + recall make it
*feel* like Narada immediately), then 3 for judgment-bearing questions.

**Latency tuning (same track):** current ~1.8 s. Try `semantic_vad` and
a shorter `silence_duration_ms` (~200 ms) to cut the ~500 ms turn-
detection window; measure again with the `av_test` harness. Brisbane →
US OpenAI adds unavoidable RTT. Target ~1.2 s to first audio.

---

## Track B — Body revival: the ESP32-S3-BOX-3 (Phase 3)

**Current state (verified 2026-08-08):** device now connected via USB —
ESP32-S3 (QFN56 rev v0.2), 16MB flash, 16MB PSRAM, native USB-JTAG, on
COM3, MAC 90:e5:b1:d6:52:48. esptool 5.2.0 talks to it. It still shows
"Host not found" — the *old Home-Assistant satellite firmware* waiting
for an HA we've abandoned; the device must be **reflashed with LiveKit
ESP32 firmware** to join our stack.

**REVERSIBLE-FLASH PROCEDURE (cross-review #5 — mandatory before erase):**
a full erase of our only device with no rollback is unacceptable.
Order:
0. **Back up the full 16MB flash and verify it** (`esptool read-flash 0
   0x1000000 backup.bin`; record chip metadata + sha256). ✅ started
   2026-08-08 → `~/.narada/esp32/box3-firmware-backup-20260808.bin`.
1. **Pin** a known-compatible ESP-IDF version and the LiveKit ESP32 SDK
   commit; record them.
2. **Validate the exact BOX-3 target + partition layout** and build the
   firmware *before* erasing anything.
3. **Document ROM download-mode recovery** (boot + IO0) and the exact
   `write-flash` / `--erase-all` rollback commands using the backup.
4. Only then flash. **Smoke-test mic, es8311/es7210 codec, speaker,
   Wi-Fi, and LiveKit connectivity** before calling it successful.

**GATE (cross-review #4): do NOT connect the flashed device to the
always-on worker until the false-accept safety gate (see Track C) exists**
— a body streaming ambient audio to a weak wake model + always-on billed
worker is the actual risk moment.

Then the original steps:

1. **Find & reach the device.** Power-cycle it; check the router's DHCP
   lease table for the ESP32/Espressif MAC (OUI 24:0A:C4, 30:AE:A4,
   7C:9E:BD, A0:76:4E, …), or scan the subnet. Give it a **DHCP
   reservation** so its IP stops drifting.
2. **Toolchain.** Install ESP-IDF (v6.x) — the flashing environment.
   `esptool`/`idf.py` were absent on this box as of 2026-08-06.
3. **Firmware.** Fetch the LiveKit ESP32 SDK BOX-3 example (official,
   Espressif-co-built). Configure: LiveKit server URL (LAN
   `ws://<pc-ip>:7880`), a room-join token, wake behaviour.
4. **Flash over USB** (physical access required — this is the human-in-
   the-loop step). The 2024 "firmware failure" was structural HA-
   dependence, not an inability to flash; deha's firmware YAMLs are the
   BOX-3 pinout/codec reference.
5. **Point it at the voice worker.** Once flashed, the BOX-3 joins a
   LiveKit room; the (hardened, auto-started) worker picks it up. Walk
   the room, confirm it hears/answers.
6. **Display/expression** (later): port deha's sprite atlas / faces /
   weather scenes to an LVGL layer in the LiveKit firmware, driven over
   data channels — the display support in the LiveKit example is
   unverified, so budget real firmware work here.

**Risk:** the LiveKit ESP32 SDK is young; xiaozhi firmware + a hacked
server is the fallback (its clone is frozen and ready).

**Wake word** (from 2026-08-07): the synthetic model scored 2/30 on
Suti's voice — retrain with **real recordings**, ideally captured
through the BOX-3 mic in the real room (matched acoustics), dropped into
`~/.narada/wakeword/data/positive_train/` before the augment stage. Do
this *after* the device is flashed so the recordings come from the real
mic.

---

## Track C — Service hardening (buildable now, device-independent)

Goal (Suti's ask): the service supporting the ESP32 **comes online with
Windows, stays online invisibly, recovers itself invisibly if it dies,
and logs full transcripts of what was said.** Most of this already
exists; the gaps are marked BUILD.

| Requirement | Current state | Action |
|---|---|---|
| Comes online with Windows | Docker Desktop auto-starts on login; LiveKit container `restart: unless-stopped`; `Narada_Host` scheduled task runs at logon | **Voice stays DISABLED until the #3 privacy + #4 false-accept gates pass.** The worker is wired into the host and proven to start/register; enabling it + verifying the reboot chain (Docker → LiveKit → host → worker) is the LAST step, after the gates. |
| Stays online, invisible | Host runs components under `pythonw` (no console); LiveKit is a headless container | Worker inherits this the moment it's enabled (post-gate). Verify no console flash then. |
| Recovers invisibly if it dies | Host supervisor restarts components with backoff; container restart policy | **STILL OPEN (cross-review #6):** process supervision catches *exits*, not a worker that stays alive but stops accepting rooms (event-loop / LiveKit-registration hang). BUILD a `health_url`/readiness endpoint, configure the host to probe it, and test restart-after-hang — not just restart-after-exit. |
| Logs full transcripts | built (this session) | **NOT SAFE YET (cross-review #3):** full plaintext conversations stored indefinitely, no consent/indicator/retention/redaction. See privacy controls below. |

**Transcript logging (built, but privacy-incomplete):** hooks the
AgentSession `conversation_item_added` event; appends each finalized
utterance with a UTC timestamp to a per-session markdown file,
fail-open. **Required before the body goes live (cross-review #3):** a
visible/audible recording indicator; owner-only file permissions or
encryption; redaction of tool-derived + sensitive content; a
retention/deletion policy; and keeping any training-data use opt-in and
*separate* from the audit log (once memory recall lands, private
memories must not bleed into the transcript tree).

**False-accept safety gate (cross-review #4):** MEASURED 2026-08-09
(`scripts/eval_wake_false_accept.py`): **0 false accepts over 2.73h of
ambient audio** and **0/13 adversarial near-phrases**, at thresholds 0.5
AND 0.32. So the ambient-audio-opens-a-billed-session risk is very low —
the model barely triggers at all (its problem is recall, not false
accepts). Recording indicator ✓ (transcript marker), budget alert ✓
(budget guards warn at 80%, fail-closed daily cap). STILL OWED before
always-on with a body: a **hardware mute** and an **authenticated-
presence / second-confirmation** signal (both device-side, Phase 3), and
real-room-mic false-accept data (needs the BOX-3 as a capture source).
Honest limitation of the current number: training-adjacent noise + TTS
near-phrases, not this room's mic.

**Done this session (2026-08-08):** transcript logging built (privacy
controls still owed); auto-start/supervision chain proven (worker starts
+ registers under the host). The voice component was briefly enabled to
verify supervision, then **RE-DISABLED** (live config + template) per
cross-review #4 — it is currently OFF and must stay OFF until the #3/#4
gates pass. Leaked orphan workers cleaned up (and the underlying
orphan-on-host-restart bug fixed 2026-08-09 via a kill-on-close job).
**Track C is PARTIAL, not done** — hung-worker liveness (#6), transcript
privacy (#3), and the false-accept gate (#4) remain; #3 and #4 are hard
prerequisites for enabling voice / connecting the body.

**Config state of record:** `voice` is `enabled: false` everywhere. Do
NOT flip it on until the false-accept gate and transcript-privacy
controls are built and tested — enabling an always-on billed/recording
worker gated only by a weak wake word is the risk this gate prevents.

---

## Cross-review (Codex, round 1 — 2026-08-08)

Verdict **needs-attention**; six findings, **all accepted** (no rejections):

| # | Sev | Finding | Disposition |
|---|-----|---------|-------------|
| 1 | high | Read-only `smriti_read` is still a disclosure boundary — anyone in earshot could make Narada speak private memories aloud; all recall goes to OpenAI | Accepted → Track A.2 rewritten as a voice-safe memory *projection* (private branches excluded by default, allowlist, authenticated confirmation for sensitive recall, adversarial tests) |
| 2 | high | Arbitrary speech → `claude -p` with full tools is an injection surface that bypasses the code-enforced mutation boundary | Accepted → Track A.3 rewritten as a sandboxed answer-only runner (no mutation tools/MCP/hooks, scoped read tools, delimited input, isolated cwd, timeout/concurrency) |
| 3 | high | Full plaintext transcripts stored indefinitely, no consent/indicator/retention/redaction (already-shipped Track C) | Accepted → Track C privacy controls specced; transcript logging marked NOT-SAFE-YET; prerequisite for the body |
| 4 | med | Always-on worker enabled on 2/30 *recall* evidence — that's false negatives; false *accepts* can open billed sessions from ambient audio | Accepted → false-accept gate specced; **GATE added: do not connect the body until it exists**; interim-safety note added |
| 5 | med | Reflash mandated with no backup/rollback — could brick the only device | Accepted → reversible-flash procedure added (backup ✅ started, pin toolchain/firmware, validate target/partitions pre-erase, ROM recovery docs, peripheral smoke-test) |
| 6 | med | Track C marked done but process supervision catches exits, not a hung-but-alive worker | Accepted → Track C downgraded to PARTIAL; liveness/readiness endpoint + probe + hang-restart test required |

**Net effect:** Track A must not be built until the memory-projection and
escalation-sandbox boundaries are designed + tested. Track C is not done.
Track B may proceed through the *reversible backup/identify* prep, but the
reflash-and-connect must wait for the safety gates.

**Round 2 (2026-08-09):** #1, #2, #3, #5, #6 verified RESOLVED. #4 flagged
again — not the fix but stale contradictory text in Track C ("enable the
voice component" / "currently harmless" wording) that conflicted with the
committed re-disable. Reconciled: Track C now states throughout that voice
is `enabled: false` and stays off until the #3/#4 gates pass; enablement +
reboot-chain verification moved to the last step. Plan is execution-ready.

## Open decisions for next time

1. Track A depth: build 1+2 (identity + smriti_read) next, or go
   straight to 3 (full escalate)?
2. Track B: confirm LiveKit-firmware route vs. xiaozhi fallback once we
   see the device on the network.
3. Wake-word real-voice retraining: PC-mic interim now, or wait for the
   BOX-3 mic (matched acoustics) after flashing?
