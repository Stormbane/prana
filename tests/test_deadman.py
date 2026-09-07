"""Deadman verification (one-process addendum, migration step 2).

Two halves of the watchdog seam:
- the supervisor's heartbeat loop writes a fresh, parseable beacon and
  stops with the shutdown event;
- deadman.ps1 makes the right call for every beacon state (fresh, stale,
  missing, exhausted, recovered) — exercised for real through
  powershell.exe in dry-run, which must never mutate state, tasks, or
  send pages.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from prana.host import supervisor as sup_mod
from prana.host.supervisor import Supervisor

DEADMAN_PS1 = Path(__file__).resolve().parent.parent / "scripts" / "install" / "deadman.ps1"

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="deadman is Windows-only")


class _StubAlerts:
    """Non-None alerts stub so Supervisor skips AlertManager init."""

    def __getattr__(self, name):  # record_* etc. — never called with no components
        return lambda *a, **k: None


# --- heartbeat loop ---------------------------------------------------

def test_heartbeat_writes_and_stops(tmp_path, monkeypatch):
    beat = tmp_path / "host.heartbeat"
    monkeypatch.setattr(sup_mod, "heartbeat_file", lambda: beat)
    monkeypatch.setattr(sup_mod, "HEARTBEAT_INTERVAL_S", 0.05)

    async def scenario():
        sup = Supervisor(components=[], alerts=_StubAlerts())
        task = asyncio.create_task(sup._heartbeat_loop())
        for _ in range(100):
            if beat.exists():
                break
            await asyncio.sleep(0.02)
        assert beat.exists(), "heartbeat file never appeared"
        first = json.loads(beat.read_text(encoding="utf-8"))
        assert abs(first["ts"] - time.time()) < 5.0
        assert first["pid"] > 0

        # It refreshes.
        for _ in range(100):
            await asyncio.sleep(0.02)
            if json.loads(beat.read_text(encoding="utf-8"))["ts"] > first["ts"]:
                break
        else:
            pytest.fail("heartbeat never refreshed")

        # Shutdown stops the loop promptly.
        sup._shutdown_event.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(scenario())


# --- deadman.ps1 decisions (dry-run through real powershell) ----------

def _run_deadman(tmp_path: Path, *, state: dict | None = None,
                 beat_age_s: float | None = None) -> str:
    """Invoke deadman.ps1 -DryRun against fabricated beacon/state files."""
    beat = tmp_path / "host.heartbeat"
    state_file = tmp_path / "deadman-state.json"
    log = tmp_path / "deadman.log"
    if beat_age_s is not None:
        beat.write_text(json.dumps({"ts": time.time() - beat_age_s, "pid": 1234}),
                        encoding="utf-8")
    if state is not None:
        state_file.write_text(json.dumps(state), encoding="utf-8")
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
         "Bypass", "-File", str(DEADMAN_PS1),
         "-HeartbeatPath", str(beat), "-StatePath", str(state_file),
         "-LogPath", str(log), "-EnvPath", str(tmp_path / "no.env"),
         "-TaskName", "Test_NoSuchTask", "-DryRun"],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    # Dry run never mutates state.
    if state is None:
        assert not state_file.exists()
    else:
        assert json.loads(state_file.read_text(encoding="utf-8")) == state
    return proc.stdout


def test_fresh_beacon_is_healthy(tmp_path):
    out = _run_deadman(tmp_path, beat_age_s=10)
    assert "healthy" in out
    assert "would-restart" not in out


def test_stale_beacon_restarts(tmp_path):
    out = _run_deadman(tmp_path, beat_age_s=600)
    assert "would-restart" in out
    assert "beacon stale" in out


def test_missing_beacon_restarts(tmp_path):
    out = _run_deadman(tmp_path)
    assert "would-restart" in out
    assert "beacon missing" in out


def test_exhausted_budget_stands_down_and_pages(tmp_path):
    now = time.time()
    out = _run_deadman(
        tmp_path, beat_age_s=600,
        state={"restarts": [now - 300, now - 200, now - 100],
               "paged_exhausted": False})
    assert "EXHAUSTED" in out
    assert "would-page" in out
    assert "would-restart" not in out


def test_exhausted_already_paged_stays_silent(tmp_path):
    now = time.time()
    out = _run_deadman(
        tmp_path, beat_age_s=600,
        state={"restarts": [now - 300, now - 200, now - 100],
               "paged_exhausted": True})
    assert "standing down" in out
    assert "would-page" not in out


def test_old_restarts_age_out_of_window(tmp_path):
    now = time.time()
    out = _run_deadman(
        tmp_path, beat_age_s=600,
        state={"restarts": [now - 7200, now - 6000, now - 5000],
               "paged_exhausted": False})
    assert "would-restart" in out  # window pruning frees the budget


def test_recovery_clears_ledger(tmp_path):
    now = time.time()
    out = _run_deadman(
        tmp_path, beat_age_s=10,
        state={"restarts": [now - 300], "paged_exhausted": True})
    assert "clearing restart ledger" in out
    assert "would-page" in out  # recovery notice


# --- mutating paths through the mock harness (diff-review next-step) --

HARNESS_PS1 = Path(__file__).resolve().parent / "deadman_mock_harness.ps1"


def _run_mocked(tmp_path: Path, *, state: dict | None = None,
                beat_age_s: float | None = None, page_fail: bool = False,
                state_path: Path | None = None) -> tuple[str, Path]:
    """Run deadman.ps1 for real (no dry-run) with cmdlets mocked."""
    beat = tmp_path / "host.heartbeat"
    state_file = state_path if state_path is not None else tmp_path / "deadman-state.json"
    env = tmp_path / "mock.env"
    env.write_text("NARADA_TELEGRAM_BOT_TOKEN=mock-token\n"
                   "TELEGRAM_ALLOWED_USERS=12345,67890\n", encoding="utf-8")
    if beat_age_s is not None:
        beat.write_text(json.dumps({"ts": time.time() - beat_age_s, "pid": 1234}),
                        encoding="utf-8")
    if state is not None:
        state_file.write_text(json.dumps(state), encoding="utf-8")
    args = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
            "Bypass", "-File", str(HARNESS_PS1),
            "-ScriptPath", str(DEADMAN_PS1),
            "-HeartbeatPath", str(beat), "-StatePath", str(state_file),
            "-LogPath", str(tmp_path / "deadman.log"), "-EnvPath", str(env)]
    if page_fail:
        args.append("-PageFail")
    proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout, state_file


def _read_state(state_file: Path) -> dict:
    # utf-8-sig: PowerShell 5.1's -Encoding utf8 writes a BOM.
    return json.loads(state_file.read_text(encoding="utf-8-sig"))


def test_mocked_restart_persists_ledger_before_acting(tmp_path):
    out, state_file = _run_mocked(tmp_path, beat_age_s=600)
    assert "MOCK-STOP MockHost" in out    # Running task: stop-first
    assert "MOCK-START MockHost" in out
    assert "MOCK-PAGE" in out             # action pages
    saved = _read_state(state_file)
    assert len(saved["restarts"]) == 1
    assert saved["paged_exhausted"] is False
    # A real (non-dry-run) tick writes the progress beacon the host
    # scheduler-observer watches.
    progress = tmp_path / "deadman.progress"
    assert progress.exists()
    beat = json.loads(progress.read_text(encoding="utf-8-sig"))
    assert abs(beat["ts"] - time.time()) < 120


def test_mocked_unwritable_ledger_refuses_restart(tmp_path):
    # A state path whose parent is a FILE cannot be created — the save
    # fails, so the deadman must refuse the restart entirely.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    out, _ = _run_mocked(tmp_path, beat_age_s=600,
                         state_path=blocker / "deadman-state.json")
    assert "REFUSING restart" in out
    assert "MOCK-STOP" not in out
    assert "MOCK-START" not in out


def test_mocked_corrupt_ledger_refuses_restart(tmp_path):
    state_file = tmp_path / "deadman-state.json"
    state_file.write_text("{ not json !!", encoding="utf-8")
    out, _ = _run_mocked(tmp_path, beat_age_s=600, state_path=state_file)
    assert "REFUSING restart" in out
    assert "MOCK-START" not in out
    # Corrupt ledger must not be silently repaired while unhealthy.
    assert state_file.read_text(encoding="utf-8") == "{ not json !!"


def test_mocked_exhaustion_latch_survives_window_ageout(tmp_path):
    now = time.time()
    out, state_file = _run_mocked(
        tmp_path, beat_age_s=600,
        state={"restarts": [now - 7200, now - 6000, now - 5000],
               "paged_exhausted": True})
    assert "exhaustion latched" in out
    assert "MOCK-START" not in out        # latch holds despite aged-out window
    assert _read_state(state_file)["paged_exhausted"] is True


def test_mocked_exhaustion_page_success_latches(tmp_path):
    now = time.time()
    out, state_file = _run_mocked(
        tmp_path, beat_age_s=600,
        state={"restarts": [now - 300, now - 200, now - 100],
               "paged_exhausted": False})
    assert "EXHAUSTED" in out
    assert "MOCK-PAGE" in out
    assert "MOCK-START" not in out
    assert _read_state(state_file)["paged_exhausted"] is True


def test_mocked_exhaustion_page_failure_does_not_latch(tmp_path):
    now = time.time()
    out, state_file = _run_mocked(
        tmp_path, beat_age_s=600, page_fail=True,
        state={"restarts": [now - 300, now - 200, now - 100],
               "paged_exhausted": False})
    assert "EXHAUSTED" in out
    assert "page FAILED" in out
    # Undelivered page must NOT latch — next run retries the notification.
    assert _read_state(state_file)["paged_exhausted"] is False


def test_mocked_empty_ledger_refuses_restart(tmp_path):
    # A zero-byte ledger (legacy truncated write, full disk) parses to
    # $null without an exception — it must refuse like corrupt state.
    state_file = tmp_path / "deadman-state.json"
    state_file.write_text("", encoding="utf-8")
    out, _ = _run_mocked(tmp_path, beat_age_s=600, state_path=state_file)
    assert "REFUSING restart" in out
    assert "MOCK-START" not in out
    assert state_file.read_text(encoding="utf-8") == ""  # not modified


def test_mocked_healthy_repairs_empty_ledger(tmp_path):
    state_file = tmp_path / "deadman-state.json"
    state_file.write_text("", encoding="utf-8")
    out, _ = _run_mocked(tmp_path, beat_age_s=10, state_path=state_file)
    assert "clearing restart ledger" in out
    saved = _read_state(state_file)
    assert saved["restarts"] == [] and saved["paged_exhausted"] is False


def test_mocked_healthy_repairs_corrupt_state(tmp_path):
    state_file = tmp_path / "deadman-state.json"
    state_file.write_text("{ not json !!", encoding="utf-8")
    out, _ = _run_mocked(tmp_path, beat_age_s=10, state_path=state_file)
    assert "clearing restart ledger" in out
    saved = _read_state(state_file)
    assert saved["restarts"] == [] and saved["paged_exhausted"] is False
