"""Hermes-fired heartbeat tick — runs one heartbeat cycle.

Wired as a Hermes `--no-agent --script` cron job: the script IS the job,
its stdout becomes the cron output. No LLM orchestrator in the loop.
This is the path-of-least-resistance shape — Hermes does scheduling,
prana does cognition.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from prana.spawn import run_hidden


SVAPNA_ROOT = Path("C:/Projects/svapna")
LORA_PATH = "models/lora/latest"
DISPLAY_IP = "192.168.86.35"


def main() -> int:
    # Strip any inherited Anthropic API key — heartbeat MUST run on the
    # Max subscription. The Apr 12-15 daemon racked up 180 cycles on API
    # billing because of an inherited env var. We've been bitten; we
    # check.
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)

    cmd = [
        sys.executable, "-u", "-m", "prana.heartbeat",
        "--once",
        "--lora-path", LORA_PATH,
        "--display-ip", DISPLAY_IP,
    ]

    proc = run_hidden(
        cmd,
        cwd=str(SVAPNA_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )

    # Surface tail of stdout (the daemon prints structured progress).
    # Cron output is truncated by Hermes anyway; trim aggressively.
    out = proc.stdout.splitlines()
    tail = out[-25:] if len(out) > 25 else out
    print("\n".join(tail))

    if proc.returncode != 0:
        print(f"\n[heartbeat-tick] non-zero exit: {proc.returncode}", file=sys.stderr)
        if proc.stderr:
            print(proc.stderr[-2000:], file=sys.stderr)
        return proc.returncode

    return 0


if __name__ == "__main__":
    sys.exit(main())
