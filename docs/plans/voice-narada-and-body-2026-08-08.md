# Voice-as-Narada, body revival, and service hardening

**Date:** 2026-08-08
**Status:** DRAFT — pick up next session
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
2. **Read-only `smriti_read` voice tool** (on-demand recall). The model
   calls it to answer "what did we decide about X" — fits the existing
   tool pattern, stays in the voice tier (read-only, no sovereignty
   change). This is the highest value-per-effort item.
3. **Real `escalate_to_narada` for substance** (full cognition). Routes
   a question to `claude -p` (which wakes with full smriti + identity),
   returns an answer for the voice to speak. This is the "hand it to
   Narada properly" line made real, and it's the viveka/prana boundary
   the whole project is organized around. Higher latency — that's the
   honest depth-vs-speed tradeoff; the voice should say "let me think
   about that properly" while it runs.

**Recommended build order:** 1 + 2 first (identity + recall make it
*feel* like Narada immediately), then 3 for judgment-bearing questions.

**Latency tuning (same track):** current ~1.8 s. Try `semantic_vad` and
a shorter `silence_duration_ms` (~200 ms) to cut the ~500 ms turn-
detection window; measure again with the `av_test` harness. Brisbane →
US OpenAI adds unavoidable RTT. Target ~1.2 s to first audio.

---

## Track B — Body revival: the ESP32-S3-BOX-3 (Phase 3)

**Current state (verified 2026-08-08):** device not reachable at
`192.168.86.35`, not in ARP. It still shows "Host not found" — that
screen is the *old Home-Assistant satellite firmware* waiting for an HA
that we've abandoned. Reviving HA is throwaway work; the device must be
**reflashed with LiveKit ESP32 firmware** to join our stack. Steps:

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
| Comes online with Windows | Docker Desktop auto-starts on login; LiveKit container `restart: unless-stopped`; `Narada_Host` scheduled task runs at logon | Enable the `voice` component so the worker starts with the rest. Confirm the full chain (Docker → LiveKit → host → worker) survives a reboot. |
| Stays online, invisible | Host runs components under `pythonw` (no console); LiveKit is a headless container | Voice worker inherits this once enabled. Verify no console flash. |
| Recovers invisibly if it dies | Host supervisor restarts components with backoff; container restart policy | Voice worker inherits supervision. Add a `health_url`/liveness so a *hung* (not crashed) worker is also restarted. |
| Logs full transcripts | **none** | **BUILD:** capture both sides of every conversation (user speech transcription + agent speech) to `~/.narada/heartbeat/voice-transcripts/{yyyy_mm}/{session}.md` with timestamps. |

**Transcript logging design:** hook the AgentSession's conversation
events in the worker; append each finalized user/agent utterance with a
UTC timestamp to a per-session markdown file. Fail-open — a transcript
write error must never drop the conversation. This doubles as the audit
trail for what the body said and heard, and as future training data for
the wake word / persona.

**Done this session (2026-08-08):** transcript logging built; voice
component enabled + supervised; auto-start chain verified. (See commits.)

---

## Open decisions for next time

1. Track A depth: build 1+2 (identity + smriti_read) next, or go
   straight to 3 (full escalate)?
2. Track B: confirm LiveKit-firmware route vs. xiaozhi fallback once we
   see the device on the network.
3. Wake-word real-voice retraining: PC-mic interim now, or wait for the
   BOX-3 mic (matched acoustics) after flashing?
