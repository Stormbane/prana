"""Soak the phone voice path with a simulated phone participant.

Usage:  python scripts/phone_soak.py [cycles]

Starts a sim phone worker (its own agent_name, ports, and rooms — it
can never receive the real phone's dispatches, and the box worker
declines akhada-phone-* rooms on its side), then per cycle: join a
fresh phone room as a human-kind participant the way the /talk page
does, dispatch the sim agent, and assert the session actually happens
in the right order:

  1. agent joins only AFTER the human is present (dispatch-follows-join)
  2. the greeting's audio track arrives (end-to-end realtime audio)
  3. when the human hangs up, the worker ends within the empty-grace
     window and the budget reservation settles

Cost note: each cycle opens a real OpenAI realtime session for a few
seconds (a greeting's worth). Keep cycle counts modest.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

SIM_AGENT_NAME = "akhada-phone-sim"
SIM_IDENTITY = "suti-phone-sim"
SIM_AGENTS_PORT = "8894"
SIM_HEALTH_PORT = "8895"

AGENT_JOIN_TIMEOUT_S = 15.0
AUDIO_TIMEOUT_S = 20.0
# phone_worker EMPTY_GRACE_S is 30; allow settle + shutdown on top
HANGUP_END_TIMEOUT_S = 60.0


def sim_env() -> dict:
    env = dict(os.environ)
    env.update({
        "AKHADA_AGENT_NAME": SIM_AGENT_NAME,
        "AKHADA_VOICE_AGENTS_PORT": SIM_AGENTS_PORT,
        "AKHADA_VOICE_HEALTH_PORT": SIM_HEALTH_PORT,
        "NARADA_SIM": "1",
    })
    return env


class CycleResult:
    def __init__(self, n: int) -> None:
        self.cycle = n
        self.ok = False
        self.failures: list[str] = []
        self.agent_join_s: float | None = None
        self.audio_s: float | None = None
        self.end_after_hangup_s: float | None = None


async def run_cycle(n: int) -> CycleResult:
    from livekit import api as lkapi, rtc

    res = CycleResult(n)
    room_name = f"akhada-phone-sim-{int(time.time() * 1000)}"
    url = os.environ["LIVEKIT_URL"]
    token = (lkapi.AccessToken()
             .with_identity(SIM_IDENTITY)
             .with_grants(lkapi.VideoGrants(
                 room_join=True, room=room_name,
                 can_publish=True, can_subscribe=True))
             .to_jwt())

    room = rtc.Room()
    agent_joined = asyncio.Event()
    agent_left = asyncio.Event()
    audio = asyncio.Event()

    @room.on("participant_connected")
    def _pc(p) -> None:
        if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
            agent_joined.set()

    @room.on("participant_disconnected")
    def _pd(p) -> None:
        if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
            agent_left.set()

    @room.on("track_subscribed")
    def _ts(track, _pub, _p) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            audio.set()

    lk = lkapi.LiveKitAPI()
    try:
        await room.connect(url, token)
        # The /talk page's ordering: join first, then ask for the agent.
        await lk.agent_dispatch.create_dispatch(
            lkapi.CreateAgentDispatchRequest(
                room=room_name, agent_name=SIM_AGENT_NAME))
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(agent_joined.wait(),
                                   timeout=AGENT_JOIN_TIMEOUT_S)
            res.agent_join_s = round(time.monotonic() - t0, 2)
        except asyncio.TimeoutError:
            res.failures.append(
                f"agent did not join within {AGENT_JOIN_TIMEOUT_S}s")
            return res
        try:
            await asyncio.wait_for(audio.wait(), timeout=AUDIO_TIMEOUT_S)
            res.audio_s = round(time.monotonic() - t0, 2)
        except asyncio.TimeoutError:
            res.failures.append(
                f"no greeting audio within {AUDIO_TIMEOUT_S}s")
            return res
        # Let a second of greeting actually flow, then hang up like the
        # page does: disconnect and expect the worker to wind down.
        await asyncio.sleep(1.5)
        t1 = time.monotonic()
        await room.disconnect()
        deadline = t1 + HANGUP_END_TIMEOUT_S
        while time.monotonic() < deadline:
            parts = await lk.room.list_participants(
                lkapi.ListParticipantsRequest(room=room_name))
            if not any(p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
                       for p in parts.participants):
                res.end_after_hangup_s = round(time.monotonic() - t1, 2)
                break
            await asyncio.sleep(2.0)
        else:
            res.failures.append(
                f"agent still in room {HANGUP_END_TIMEOUT_S}s after hangup")
            return res
        res.ok = True
        return res
    finally:
        try:
            if room.connection_state != rtc.ConnectionState.CONN_DISCONNECTED:
                await room.disconnect()
        except Exception:
            pass
        try:
            await lk.room.delete_room(
                lkapi.DeleteRoomRequest(room=room_name))
        except Exception:
            pass
        await lk.aclose()


async def main(cycles: int) -> int:
    from dotenv import load_dotenv
    load_dotenv(Path.home() / ".narada" / ".livekit.env")
    load_dotenv(Path.home() / ".narada" / ".voice.env")

    print("starting sim phone worker...")
    worker = subprocess.Popen(
        [sys.executable, "-m", "prana.voice.phone_worker", "start"],
        env=sim_env(),
        cwd=str(Path(__file__).resolve().parents[1]),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        await asyncio.sleep(8)  # registration
        if worker.poll() is not None:
            print("sim phone worker died on startup — aborting")
            return 2

        results = []
        for i in range(cycles):
            print(f"cycle {i + 1}/{cycles} ...", end=" ", flush=True)
            r = await run_cycle(i + 1)
            results.append(r)
            print("OK" if r.ok else f"FAIL {r.failures}")
            await asyncio.sleep(2)

        # The ledger must hold no orphan reservations afterwards.
        from prana.voice.budget import VoiceBudget
        leftover = VoiceBudget()._load().get("reservations", {})
        if leftover:
            print(f"WARNING: unsettled reservations remain: {leftover}")

        passed = sum(1 for r in results if r.ok)
        report = {
            "cycles": cycles,
            "passed": passed,
            "failed": cycles - passed,
            "unsettled_reservations": list(leftover),
            "results": [vars(r) for r in results],
            "finished": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        out = (Path.home() / ".narada" / "heartbeat"
               / "phone-soak-latest.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        # A leftover reservation is a failed invariant, not a footnote
        # (Codex review): the settlement claim is part of what "clean"
        # means here.
        clean = passed == cycles and not leftover
        print(f"\n{passed}/{cycles} cycles clean"
              + ("" if not leftover else " BUT reservations unsettled")
              + f" — report: {out}")
        return 0 if clean else 1
    finally:
        worker.terminate()
        try:
            worker.wait(timeout=10)
        except subprocess.TimeoutExpired:
            worker.kill()


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    raise SystemExit(asyncio.run(main(n)))
