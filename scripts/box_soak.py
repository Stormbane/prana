"""Soak the tap loop with the box emulator against a SIM room.

Usage:  python scripts/box_soak.py [cycles]

Starts a second voice worker in sim harness (own room, own identity,
own ports, NARADA_SIM=1 so it never pages A3 or pulses the real box's
serial line, declines jobs for foreign rooms), then drives the
emulator through tap lifecycles across scenarios and prints a report.

The real body's worker keeps running untouched — job isolation is the
request_fnc room filter on both sides. Sim transcripts are filtered
out of real continuity by room name.

Cost note: every cycle opens a real OpenAI realtime session for a few
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

SIM_ROOM = "narada-body-sim"
SIM_IDENTITY = "narada-box3-sim"
SIM_HEALTH_PORT = "8892"
SIM_AGENTS_PORT = "8893"

SCENARIOS = ["normal", "double-tap", "stop-during-greeting", "tap-spam"]


def sim_env() -> dict:
    env = dict(os.environ)
    env.update({
        "NARADA_VOICE_ROOM": SIM_ROOM,
        "NARADA_DEVICE_IDENTITY": SIM_IDENTITY,
        "NARADA_VOICE_HEALTH_PORT": SIM_HEALTH_PORT,
        "NARADA_VOICE_AGENTS_PORT": SIM_AGENTS_PORT,
        "NARADA_SIM": "1",
        "NARADA_MUSIC": "off",
        "NARADA_WAKE_GATING": "off",  # tap-only admission in the sim
    })
    return env


async def main(cycles: int) -> int:
    from dotenv import load_dotenv
    load_dotenv(Path.home() / ".narada" / ".livekit.env")
    load_dotenv(Path.home() / ".narada" / ".voice.env")

    cfg = {
        "url": os.environ["LIVEKIT_URL"],
        "api_key": os.environ["LIVEKIT_API_KEY"],
        "api_secret": os.environ["LIVEKIT_API_SECRET"],
        "room": SIM_ROOM,
        "identity": SIM_IDENTITY,
    }

    print(f"starting sim worker (room={SIM_ROOM})...")
    worker = subprocess.Popen(
        [sys.executable, "-m", "prana.voice.worker", "start"],
        env=sim_env(),
        cwd=str(Path(__file__).resolve().parents[1]),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        await asyncio.sleep(8)  # registration
        if worker.poll() is not None:
            print("sim worker died on startup — aborting")
            return 2

        from prana.voice.emulator import run_cycle

        results = []
        for i in range(cycles):
            scenario = SCENARIOS[i % len(SCENARIOS)]
            print(f"cycle {i + 1}/{cycles} [{scenario}] ...",
                  end=" ", flush=True)
            r = await run_cycle(cfg, i + 1, scenario)
            results.append(r)
            print("OK" if r.ok else f"FAIL {r.failures}")
            # Let the recycle finish + fresh job arm before rejoining
            # (the real box's WiFi takes a few seconds too).
            await asyncio.sleep(4)

        passed = sum(1 for r in results if r.ok)
        report = {
            "cycles": cycles,
            "passed": passed,
            "failed": cycles - passed,
            "results": [vars(r) for r in results],
            "finished": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        out = (Path.home() / ".narada" / "heartbeat"
               / "box-soak-latest.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\n{passed}/{cycles} cycles clean — report: {out}")
        return 0 if passed == cycles else 1
    finally:
        worker.terminate()
        try:
            worker.wait(timeout=10)
        except subprocess.TimeoutExpired:
            worker.kill()
        # Leave no sim room behind holding a phantom device.
        try:
            from livekit import api as lkapi
            lk = lkapi.LiveKitAPI()
            try:
                await lk.room.delete_room(
                    lkapi.DeleteRoomRequest(room=SIM_ROOM))
            finally:
                await lk.aclose()
        except Exception:
            pass


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    raise SystemExit(asyncio.run(main(n)))
