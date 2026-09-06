"""job-watch.ps1 verification (one-process addendum, migration step 4a).

External scheduled-job freshness watcher: healthy jobs stay silent, a
failed/overdue/missing job pages once and latches, a still-broken job
stays silent, recovery clears the latch and announces. Driven through
real powershell.exe with Get-ScheduledTaskInfo + Invoke-RestMethod
shadowed so the page/latch paths execute.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="job-watch is Windows-only")


def _ago(hours: float) -> str:
    """Absolute UTC ISO timestamp `hours` in the past (stable last-run)."""
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts" / "install"
JOB_WATCH_PS1 = _SCRIPTS / "job-watch.ps1"
HARNESS_PS1 = Path(__file__).resolve().parent / "jobwatch_mock_harness.ps1"


def _run(tmp_path: Path, *, tasks: dict, config: list, state: dict | None = None,
         page_fail: bool = False) -> tuple[str, dict]:
    cfg = tmp_path / "job-watch.json"
    cfg.write_text(json.dumps({"jobs": config}), encoding="utf-8")
    state_file = tmp_path / "job-watch-state.json"
    if state is not None:
        state_file.write_text(json.dumps(state), encoding="utf-8")
    env = tmp_path / "mock.env"
    env.write_text("NARADA_TELEGRAM_BOT_TOKEN=tok\nTELEGRAM_ALLOWED_USERS=999\n", encoding="utf-8")
    args = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
            "Bypass", "-File", str(HARNESS_PS1),
            "-ScriptPath", str(JOB_WATCH_PS1), "-ConfigPath", str(cfg),
            "-StatePath", str(state_file), "-LogPath", str(tmp_path / "d.log"),
            "-EnvPath", str(env), "-TasksJson", json.dumps(tasks)]
    if page_fail:
        args.append("-PageFail")
    proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    saved = {}
    if state_file.exists():
        saved = json.loads(state_file.read_text(encoding="utf-8-sig"))
    return proc.stdout, saved


def test_healthy_job_is_silent(tmp_path):
    out, saved = _run(tmp_path,
                      tasks={"smriti-nightly": {"LastTaskResult": 0, "LastRunIso": _ago(2)}},
                      config=[{"task": "smriti-nightly", "max_age_hours": 26}])
    assert "ok" in out
    assert "MOCK-PAGE" not in out
    assert saved == {}


def test_failed_job_pages_and_latches(tmp_path):
    out, saved = _run(tmp_path,
                      tasks={"smriti-nightly": {"LastTaskResult": 1, "LastRunIso": _ago(2)}},
                      config=[{"task": "smriti-nightly", "max_age_hours": 26}])
    assert "FAILED (result 1)" in out
    assert "MOCK-PAGE" in out
    assert "smriti-nightly" in saved


def test_overdue_job_pages(tmp_path):
    out, saved = _run(tmp_path,
                      tasks={"smriti-nightly": {"LastTaskResult": 0, "LastRunIso": _ago(40)}},
                      config=[{"task": "smriti-nightly", "max_age_hours": 26}])
    assert "OVERDUE" in out
    assert "MOCK-PAGE" in out
    assert "smriti-nightly" in saved


def test_missing_task_pages(tmp_path):
    out, saved = _run(tmp_path,
                      tasks={"smriti-nightly": "MISSING"},
                      config=[{"task": "smriti-nightly", "max_age_hours": 26}])
    assert "not found in the scheduler" in out
    assert "MOCK-PAGE" in out
    assert saved.get("smriti-nightly") == "missing"


def test_running_task_not_treated_as_failure(tmp_path):
    # 267009 = TASK_STATE_RUNNING reported as last result mid-run.
    out, saved = _run(tmp_path,
                      tasks={"smriti-nightly": {"LastTaskResult": 267009, "LastRunIso": _ago(0.1)}},
                      config=[{"task": "smriti-nightly", "max_age_hours": 26}])
    assert "MOCK-PAGE" not in out
    assert saved == {}


def test_still_broken_stays_silent(tmp_path):
    # State already latched with the SAME signature the run will compute.
    # AgeHours 2 -> LastRunTime ~2h ago; signature is result=1@<ticks>,
    # which we cannot predict, so assert via a second run instead.
    tasks = {"smriti-nightly": {"LastTaskResult": 1, "LastRunIso": _ago(2)}}
    config = [{"task": "smriti-nightly", "max_age_hours": 26}]
    _, saved1 = _run(tmp_path, tasks=tasks, config=config)
    out2, saved2 = _run(tmp_path, tasks=tasks, config=config, state=saved1)
    # Second run with the carried latch: no new page.
    assert "already paged, standing down" in out2
    assert "MOCK-PAGE" not in out2
    assert saved2 == saved1


def test_recovery_clears_latch_and_announces(tmp_path):
    out, saved = _run(tmp_path,
                      tasks={"smriti-nightly": {"LastTaskResult": 0, "LastRunIso": _ago(2)}},
                      config=[{"task": "smriti-nightly", "max_age_hours": 26}],
                      state={"smriti-nightly": "result=1@123"})
    assert "recovered" in out
    assert "MOCK-PAGE" in out
    assert saved == {}  # latch cleared


def test_failed_page_does_not_latch(tmp_path):
    out, saved = _run(tmp_path,
                      tasks={"smriti-nightly": {"LastTaskResult": 1, "LastRunIso": _ago(2)}},
                      config=[{"task": "smriti-nightly", "max_age_hours": 26}],
                      page_fail=True)
    assert "page FAILED" in out
    assert saved.get("smriti-nightly") == ""  # empty = will retry next tick


def test_overdue_only_ignores_nonzero_exit(tmp_path):
    # smriti-morning exits 1 on "already sent today" — overdue-only must
    # NOT treat that as a failure, but a recent run means not overdue.
    out, saved = _run(tmp_path,
                      tasks={"smriti-morning": {"LastTaskResult": 1, "LastRunIso": _ago(2)}},
                      config=[{"task": "smriti-morning", "max_age_hours": 26,
                               "check": "overdue-only"}])
    assert "MOCK-PAGE" not in out
    assert "ok" in out
    assert saved == {}


def test_overdue_only_still_catches_a_stalled_job(tmp_path):
    # overdue-only ignores the exit code but still pages if it stops firing.
    out, saved = _run(tmp_path,
                      tasks={"smriti-morning": {"LastTaskResult": 0, "LastRunIso": _ago(40)}},
                      config=[{"task": "smriti-morning", "max_age_hours": 26,
                               "check": "overdue-only"}])
    assert "OVERDUE" in out
    assert "MOCK-PAGE" in out
    assert "smriti-morning" in saved


def test_multiple_jobs_independent(tmp_path):
    out, saved = _run(
        tmp_path,
        tasks={"smriti-nightly": {"LastTaskResult": 0, "LastRunIso": _ago(2)},
               "smriti-morning": {"LastTaskResult": 1, "LastRunIso": _ago(2)},
               "smriti-logd": {"LastTaskResult": 0, "LastRunIso": _ago(5)}},
        config=[{"task": "smriti-nightly", "max_age_hours": 26},
                {"task": "smriti-morning", "max_age_hours": 26},
                {"task": "smriti-logd", "max_age_hours": 0.5}])
    # nightly ok, morning failed, logd overdue (5h > 0.5h)
    assert "smriti-morning" in saved
    assert "smriti-logd" in saved
    assert "smriti-nightly" not in saved
