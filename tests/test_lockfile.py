"""Tests for prana.host.lockfile — focused on stale-lock recovery."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from prana.host import lockfile as lf


# ── helpers ───────────────────────────────────────────────────────────


@pytest.fixture
def tmp_lockfile(tmp_path, monkeypatch):
    """Point the module at a throwaway lock path for the duration of one test."""
    target = tmp_path / "host.lock"
    monkeypatch.setattr(lf, "lockfile_path", lambda: target)
    return target


def _write_lock(path, *, pid: int, start_time: str | None) -> None:
    payload = {"pid": pid, "kind": "prana.host", "argv": []}
    if start_time is not None:
        payload["start_time"] = start_time
    path.write_text(json.dumps(payload), encoding="utf-8")


# ── _lock_holder_alive ────────────────────────────────────────────────


def test_holder_alive_when_pid_dead():
    """Bare case: pid not alive -> holder not alive, regardless of start_time."""
    with patch.object(lf, "_pid_alive", return_value=False):
        assert lf._lock_holder_alive({"pid": 12345, "start_time": "2026-01-01T00:00:00+00:00"}) is False


def test_holder_alive_when_pid_alive_no_start_time_field():
    """Backward compat: lockfile without start_time falls back to pid-only."""
    with patch.object(lf, "_pid_alive", return_value=True):
        assert lf._lock_holder_alive({"pid": 12345}) is True


def test_holder_alive_when_start_time_matches():
    """Same pid + matching start_time => same process => holder alive."""
    if not lf.HAS_PSUTIL:
        pytest.skip("requires psutil")

    recorded = datetime(2026, 5, 16, 12, 41, 56, tzinfo=timezone.utc)
    with patch.object(lf, "_pid_alive", return_value=True), \
         patch.object(lf.psutil, "Process") as mock_proc:
        mock_proc.return_value.create_time.return_value = recorded.timestamp()
        assert lf._lock_holder_alive(
            {"pid": 7260, "start_time": recorded.isoformat()}
        ) is True


def test_holder_alive_false_when_pid_reused():
    """Pid alive but start_time disagrees => PID has been reused. Not the holder."""
    if not lf.HAS_PSUTIL:
        pytest.skip("requires psutil")

    recorded = datetime(2026, 5, 16, 12, 41, 56, tzinfo=timezone.utc)
    # The actual process at this pid started yesterday — well outside tolerance.
    actual = datetime(2026, 5, 17, 16, 50, 0, tzinfo=timezone.utc)
    with patch.object(lf, "_pid_alive", return_value=True), \
         patch.object(lf.psutil, "Process") as mock_proc:
        mock_proc.return_value.create_time.return_value = actual.timestamp()
        assert lf._lock_holder_alive(
            {"pid": 7260, "start_time": recorded.isoformat()}
        ) is False


def test_holder_alive_tolerates_small_clock_skew():
    """A few seconds between process start and lockfile write is normal."""
    if not lf.HAS_PSUTIL:
        pytest.skip("requires psutil")

    recorded = datetime(2026, 5, 16, 12, 41, 56, tzinfo=timezone.utc)
    actual = recorded.timestamp() - 2.0  # process started 2s before lockfile write
    with patch.object(lf, "_pid_alive", return_value=True), \
         patch.object(lf.psutil, "Process") as mock_proc:
        mock_proc.return_value.create_time.return_value = actual
        assert lf._lock_holder_alive(
            {"pid": 7260, "start_time": recorded.isoformat()}
        ) is True


def test_holder_alive_handles_unparseable_start_time():
    """Garbage start_time => fall back to pid-only check, don't crash."""
    with patch.object(lf, "_pid_alive", return_value=True):
        assert lf._lock_holder_alive({"pid": 7260, "start_time": "not-a-date"}) is True


# ── acquire integration ───────────────────────────────────────────────


def test_acquire_succeeds_when_lockfile_pid_reused(tmp_lockfile):
    """The bug we just fixed: pid is alive (reused) but start_time mismatch.
    Should treat as stale and acquire."""
    if not lf.HAS_PSUTIL:
        pytest.skip("requires psutil")

    recorded = "2026-05-16T12:41:56+00:00"
    _write_lock(tmp_lockfile, pid=7260, start_time=recorded)

    # Simulate: pid 7260 exists but as a totally different process.
    with patch.object(lf, "_pid_alive", return_value=True), \
         patch.object(lf.psutil, "Process") as mock_proc:
        mock_proc.return_value.create_time.return_value = (
            datetime(2026, 5, 17, 16, 50, 0, tzinfo=timezone.utc).timestamp()
        )
        assert lf.acquire() is True

    # Our pid should now be in the lockfile.
    new = json.loads(tmp_lockfile.read_text(encoding="utf-8"))
    assert new["pid"] == os.getpid()


def test_acquire_refuses_when_holder_really_alive(tmp_lockfile):
    """Genuine double-launch: same pid AND matching start_time. Refuse."""
    if not lf.HAS_PSUTIL:
        pytest.skip("requires psutil")

    recorded_dt = datetime.now(timezone.utc)
    _write_lock(tmp_lockfile, pid=9999, start_time=recorded_dt.isoformat())

    with patch.object(lf, "_pid_alive", return_value=True), \
         patch.object(lf.psutil, "Process") as mock_proc:
        mock_proc.return_value.create_time.return_value = recorded_dt.timestamp()
        assert lf.acquire() is False


def test_acquire_succeeds_when_pid_dead(tmp_lockfile):
    """Classic stale lock: pid simply gone."""
    _write_lock(tmp_lockfile, pid=99999, start_time="2026-05-16T12:41:56+00:00")
    with patch.object(lf, "_pid_alive", return_value=False):
        assert lf.acquire() is True
