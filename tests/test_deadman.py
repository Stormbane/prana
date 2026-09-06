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
