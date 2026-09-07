"""cron_run.py: the child must run EXACTLY ONCE even if logging fails
after it (Codex review — daily-debrief delivers on each run)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

CRON_RUN = Path(__file__).resolve().parent.parent / "scripts" / "cron_run.py"


def _counter_script(tmp_path: Path) -> tuple[Path, Path]:
    """A child script that appends one line each time it runs, so we can
    count invocations."""
    counter = tmp_path / "count.txt"
    script = tmp_path / "child.py"
    script.write_text(
        "from pathlib import Path\n"
        f"p = Path(r'{counter}')\n"
        "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')\n"
        "import sys; sys.exit(0)\n",
        encoding="utf-8")
    return script, counter


def test_child_runs_once_normally(tmp_path):
    script, counter = _counter_script(tmp_path)
    log = tmp_path / "job.log"
    rc = subprocess.run([sys.executable, str(CRON_RUN), str(log), str(script)],
                        capture_output=True, text=True, timeout=60).returncode
    assert rc == 0
    assert counter.read_text() == "1"
    assert log.exists() and "run:" in log.read_text(encoding="utf-8-sig")


def test_child_runs_once_when_log_dir_unwritable(tmp_path):
    # Log path whose parent is a FILE — open() fails; the job must still
    # run, and EXACTLY ONCE (no fallback double-invocation).
    script, counter = _counter_script(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    bad_log = blocker / "job.log"
    rc = subprocess.run([sys.executable, str(CRON_RUN), str(bad_log), str(script)],
                        capture_output=True, text=True, timeout=60).returncode
    assert rc == 0
    assert counter.read_text() == "1"  # ran exactly once despite no log


def test_exit_code_propagates(tmp_path):
    script = tmp_path / "child.py"
    script.write_text("import sys; sys.exit(23)\n", encoding="utf-8")
    log = tmp_path / "job.log"
    rc = subprocess.run([sys.executable, str(CRON_RUN), str(log), str(script)],
                        capture_output=True, text=True, timeout=60).returncode
    assert rc == 23
