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

    # Open the log destination BEFORE running the child, so the run
    # decision is made once. A log that cannot be opened degrades to no
    # capture (stdout inherited) — the job still runs, never twice.
    log_f = None
    try:
        log_f = open(log_path, "a", encoding="utf-8", errors="replace")
        log_f.write(f"\n=== {stamp} run: {' '.join(sys.argv[2:])} ===\n")
        log_f.flush()
    except OSError:
        if log_f is not None:
            try:
                log_f.close()
            except OSError:
                pass
        log_f = None

    # The child runs EXACTLY ONCE here. Nothing after this may re-invoke
    # it — a footer/close failure must never cause a second delivery
    # (Codex review): daily-debrief delivers a message on each run.
    try:
        rc = subprocess.run(
            child_cmd,
            stdout=log_f if log_f is not None else None,
            stderr=subprocess.STDOUT if log_f is not None else None,
        ).returncode
    finally:
        if log_f is not None:
            try:
                log_f.write(f"=== {stamp} exit rc={rc} ===\n")
            except (OSError, NameError):
                pass  # rc may be unset only if run() raised — re-raised below
            try:
                log_f.close()
            except OSError:
                pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
