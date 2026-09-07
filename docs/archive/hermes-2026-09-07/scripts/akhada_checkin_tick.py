"""Hermes-fired Akhada check-in tick — thin shim over prana's script.

`--no-agent --script` job: this script IS the job. The real logic
(akhada's worth-a-nudge decision, prana's body/Telegram router, the
dedupe record) lives in the prana repo where it is versioned and
tested: C:/Projects/prana/scripts/akhada_checkin_tick.py
"""

import subprocess
import sys

TICK = "C:/Projects/prana/scripts/akhada_checkin_tick.py"


def main() -> int:
    proc = subprocess.run([sys.executable, TICK],
                          capture_output=True, text=True, timeout=120)
    if proc.stdout:
        print(proc.stdout.strip())
    if proc.returncode != 0:
        print(f"akhada check-in tick failed rc={proc.returncode}: "
              f"{(proc.stderr or '')[:300]}")
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
