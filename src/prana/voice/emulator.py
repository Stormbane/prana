"""Box emulator — a software BOX-3 for soak-testing the tap loop.

Born from field round 16 (Suti: "different issues every time... we're
in real trouble getting tap-to-talk working, aren't we?"). Until now
every fix shipped and waited for his finger; this emulator runs the
tap -> session -> greeting -> sleep -> recycle cycle unattended, so
races and wedges surface on the bench instead of in the lounge room.

It speaks the firmware's exact protocol: joins the room with a device
identity, publishes a (silent) mic track, echoes the admission nonce
in a wake tap, counts agent audio frames as greeting proof, sends the
bare sleep tap, and rides the room recycle like the box does — full
disconnect, rejoin, fresh dispatch.

SCOPE (honest): this exercises the worker/LiveKit/OpenAI loop — the
layer where most of this week's bugs lived. It does NOT run the ESP32
firmware, so SDK-level wedges (SCTP storms, renegotiation deafness)
still need the physical box.

Never point it at the real body's room: same-identity joins kick the
real box. The soak script (scripts/box_soak.py) runs a second worker
against a sim room with NARADA_SIM=1.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field

logger = logging.getLogger("box-emulator")

TOPIC_TAP = "narada.tap"
TOPIC_ADMISSION = "narada.admission"
TOPIC_SESSION = "narada.session"

SAMPLE_RATE = 24000
FRAME_MS = 10


@dataclass
class CycleResult:
    cycle: int
    scenario: str
    ok: bool = False
    t_nonce_s: float | None = None
    t_open_s: float | None = None
    greeting_frames: int = 0
    t_recycle_s: float | None = None
    failures: list[str] = field(default_factory=list)


class BoxEmulator:
    """One connect-to-recycle lifetime, like a firmware boot."""

    def __init__(self, url: str, api_key: str, api_secret: str,
                 room_name: str, identity: str):
        self.url = url
        self.api_key = api_key
        self.api_secret = api_secret
        self.room_name = room_name
        self.identity = identity
        self.nonce: str | None = None
        self.session_open = asyncio.Event()
        self.session_closed = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.agent_frames = 0
        self._room = None
        self._pump_task = None
        self._stream_tasks: list = []

    def _token(self) -> str:
        from livekit import api as lkapi
        return (lkapi.AccessToken(self.api_key, self.api_secret)
                .with_identity(self.identity)
                .with_name(self.identity)
                .with_grants(lkapi.VideoGrants(
                    room_join=True, room=self.room_name,
                    can_publish=True, can_subscribe=True,
                    can_publish_data=True))
                .to_jwt())

    async def connect(self) -> None:
        from livekit import rtc
        self._room = rtc.Room()

        @self._room.on("data_received")
        def _on_data(packet) -> None:
            try:
                topic = getattr(packet, "topic", "")
                data = packet.data
                msg = json.loads(data.decode("utf-8", "replace"))
            except Exception:
                return
            if topic == TOPIC_ADMISSION and msg.get("type") == "admission_nonce":
                self.nonce = msg.get("nonce")
            elif topic == TOPIC_SESSION and isinstance(msg, dict):
                if "open" in msg:
                    if msg["open"]:
                        self.session_open.set()
                    else:
                        self.session_closed.set()

        @self._room.on("track_subscribed")
        def _on_track(track, publication, participant) -> None:
            from livekit import rtc as _rtc
            if track.kind != _rtc.TrackKind.KIND_AUDIO:
                return

            async def _count() -> None:
                stream = _rtc.AudioStream(track)
                try:
                    async for _ev in stream:
                        self.agent_frames += 1
                finally:
                    await stream.aclose()

            self._stream_tasks.append(asyncio.ensure_future(_count()))

        @self._room.on("disconnected")
        def _on_disc(*_a) -> None:
            self.disconnected.set()

        await self._room.connect(self.url, self._token())

        # Publish the "mic": a silent track, pumped like a real one.
        from livekit import rtc
        source = rtc.AudioSource(SAMPLE_RATE, 1)
        track = rtc.LocalAudioTrack.create_audio_track("mic", source)
        opts = rtc.TrackPublishOptions(
            source=rtc.TrackSource.SOURCE_MICROPHONE)
        await self._room.local_participant.publish_track(track, opts)

        samples = SAMPLE_RATE * FRAME_MS // 1000

        async def _pump() -> None:
            from livekit import rtc as _rtc
            frame = _rtc.AudioFrame.create(SAMPLE_RATE, 1, samples)
            while not self.disconnected.is_set():
                await source.capture_frame(frame)
                await asyncio.sleep(FRAME_MS / 1000)

        self._pump_task = asyncio.ensure_future(_pump())

    async def send_tap(self, wake: bool) -> None:
        payload: dict = {"type": "tap"}
        if wake:
            payload["nonce"] = self.nonce
            payload["tier"] = "personal"
        await self._room.local_participant.publish_data(
            json.dumps(payload).encode(), topic=TOPIC_TAP, reliable=True)

    async def close(self) -> None:
        for t in self._stream_tasks:
            t.cancel()
        if self._pump_task is not None:
            self._pump_task.cancel()
        try:
            if self._room is not None:
                await self._room.disconnect()
        except Exception:
            pass


async def wait_for(event: asyncio.Event, timeout: float) -> bool:
    try:
        await asyncio.wait_for(event.wait(), timeout)
        return True
    except asyncio.TimeoutError:
        return False


async def run_cycle(cfg: dict, cycle: int, scenario: str) -> CycleResult:
    """One full tap lifecycle under the given scenario."""
    r = CycleResult(cycle=cycle, scenario=scenario)
    emu = BoxEmulator(cfg["url"], cfg["api_key"], cfg["api_secret"],
                      cfg["room"], cfg["identity"])
    t0 = time.monotonic()
    try:
        await emu.connect()

        # 1. admission nonce (a fresh job publishes one on arming)
        deadline = time.monotonic() + 25
        while emu.nonce is None and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        if emu.nonce is None:
            r.failures.append("no admission nonce within 25s")
            return r
        r.t_nonce_s = round(time.monotonic() - t0, 2)

        # 2. wake tap (scenario flavors)
        await emu.send_tap(wake=True)
        if scenario == "double-tap":
            await asyncio.sleep(0.3)
            await emu.send_tap(wake=True)   # firmware debounce absent
        elif scenario == "tap-spam":
            for _ in range(4):
                await asyncio.sleep(0.4)
                await emu.send_tap(wake=True)

        # 3. session opens
        if not await wait_for(emu.session_open, 25):
            r.failures.append("session never opened within 25s of tap")
            return r
        r.t_open_s = round(time.monotonic() - t0, 2)

        # 4. greeting audio actually arrives
        deadline = time.monotonic() + 20
        while emu.agent_frames < 50 and time.monotonic() < deadline:
            await asyncio.sleep(0.25)
        r.greeting_frames = emu.agent_frames
        if emu.agent_frames < 50:
            r.failures.append(
                f"greeting audio missing ({emu.agent_frames} frames)")

        if scenario == "stop-during-greeting":
            pass  # sleep tap goes out immediately below
        else:
            await asyncio.sleep(2.0)

        # 5. sleep tap -> retire -> room recycle (we get disconnected)
        await emu.send_tap(wake=False)
        if not await wait_for(emu.disconnected, 20):
            r.failures.append("no room recycle within 20s of sleep tap")
            return r
        r.t_recycle_s = round(time.monotonic() - t0, 2)
        r.ok = not r.failures
        return r
    except Exception as exc:  # pragma: no cover - soak diagnostics
        r.failures.append(f"exception: {exc!r}")
        return r
    finally:
        await emu.close()
