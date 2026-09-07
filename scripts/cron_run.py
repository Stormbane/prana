"""Cron launcher: run a scheduled job's script and capture its output.

Replaces Hermes's stdout->local-log capture for the Windows-task-based
cron jobs (retire-hermes migration §Logging). A scheduled task's action
is `run-hidden.vbs python cron_run.py <logpath> <script> [args...]`:
run-hidden.vbs suppresses the console, cron_run.py redirects the child's
stdout+stderr to a durable per-job log and propagates its exit code so
`LastTaskResult` is the real job result.

Python (not a cmd/vbs redirect) does this with no shell-quoting hazard.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_LOG_CAP_BYTES = 5 * 1024 * 1024  # rotate at 5 MB (keep one .1 backup)


def main() -> int:
    if len(sys.argv) < 3:
        sys.stderr.write("usage: cron_run.py <logpath> <script> [args...]\n")
        return 87  # ERROR_INVALID_PARAMETER
    log_path = Path(sys.argv[1])
    child_cmd = [sys.executable, *sys.argv[2:]]

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if log_path.exists() and log_path.stat().st_size > _LOG_CAP_BYTES:
            backup = log_path.with_suffix(log_path.suffix + ".1")
            backup.unlink(missing_ok=True)
            log_path.rename(backup)
    except OSError:
        pass  # logging must never block the job

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with open(log_path, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"\n=== {stamp} run: {' '.join(sys.argv[2:])} ===\n")
            f.flush()
            rc = subprocess.run(child_cmd, stdout=f,
                                stderr=subprocess.STDOUT).returncode
            f.write(f"=== {stamp} exit rc={rc} ===\n")
        return rc
    except OSError:
        # Log unwritable — still run the job so the work is not skipped;
        # its exit code remains the source of truth.
        return subprocess.run(child_cmd).returncode


if __name__ == "__main__":
    sys.exit(main())
