"""Tests for prana.spawn."""

from __future__ import annotations

import os
import subprocess
import sys
from unittest.mock import patch

import pytest

from prana.spawn import popen_hidden, run_hidden


def test_run_hidden_executes_simple_command():
    proc = run_hidden(
        [sys.executable, "-c", "print('hi')"],
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "hi"


def test_run_hidden_refuses_shell_true():
    with pytest.raises(ValueError, match="shell=True"):
        run_hidden(["echo", "hi"], shell=True)


def test_popen_hidden_refuses_shell_true():
    with pytest.raises(ValueError, match="shell=True"):
        popen_hidden(["echo", "hi"], shell=True)


def test_run_hidden_sets_create_no_window_on_windows():
    """The whole point: CREATE_NO_WINDOW is merged into creationflags
    on Windows so console-less parents don't allocate a window."""
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args=args[0], returncode=0)

    with patch("prana.spawn.subprocess.run", side_effect=fake_run):
        run_hidden(["x"])

    if os.name == "nt":
        assert captured.get("creationflags", 0) & subprocess.CREATE_NO_WINDOW
    else:
        # POSIX: helper should be a no-op for creationflags
        assert captured.get("creationflags", 0) == 0


def test_run_hidden_preserves_existing_creationflags_on_windows():
    """If the caller already passes creationflags, ours OR-merges in."""
    if os.name != "nt":
        pytest.skip("creationflags merging is Windows-only")

    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args=args[0], returncode=0)

    sentinel = subprocess.CREATE_NEW_PROCESS_GROUP  # any other flag
    with patch("prana.spawn.subprocess.run", side_effect=fake_run):
        run_hidden(["x"], creationflags=sentinel)

    flags = captured["creationflags"]
    assert flags & subprocess.CREATE_NO_WINDOW
    assert flags & sentinel
