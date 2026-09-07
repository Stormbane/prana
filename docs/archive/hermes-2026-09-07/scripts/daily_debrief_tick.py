"""Hermes-fired daily debrief tick — thin shim over prana's script.

`--no-agent --script` job: this script IS the job. The real logic
(gather material, claude -p on the subscription, deliver via the state
router) lives in the prana repo where it is versioned and tested:
C:/Projects/prana/scripts/daily_debrief.py
"""

import subprocess
import sys

DEBRIEF = "C:/Projects/prana/scripts/daily_debrief.py"


def main() -> int:
    proc = subprocess.run([sys.executable, DEBRIEF],
                          capture_output=True, text=True, timeout=600)
    if proc.stdout:
        print(proc.stdout.strip())
    if proc.returncode != 0:
        print(f"debrief failed rc={proc.returncode}: "
              f"{(proc.stderr or '')[:300]}")
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
