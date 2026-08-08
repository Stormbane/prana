"""The host must never orphan component processes.

The host's kill-on-close JobObject ties every spawned component to the
host's lifetime: when the host dies (however it dies — including
Stop-ScheduledTask killing it without a graceful shutdown), Windows
reaps all children. This test proves a ComponentRunner assigns its
child to the job and the job reaps it.
"""

from __future__ import annotations

import subprocess
import sys
import time

import psutil
import pytest

from prana.host.component import Component
from prana.host.supervisor import ComponentRunner, Supervisor
from prana.sessions.jobobject import JobObject


def test_supervisor_creates_a_job_and_shares_it():
    comp = Component(name="x", command=["python", "-c", "pass"], cwd=".")
    sup = Supervisor([comp])
    # every runner references the same host job
    assert sup._job is not None
    assert all(r._job is sup._job for r in sup.runners.values())


@pytest.mark.skipif(sys.platform != "win32", reason="Job Objects are Windows")
def test_child_assigned_to_job_dies_when_job_killed(tmp_path):
    """A process assigned to the host job is reaped when the job closes —
    exactly what protects components from orphaning on host death."""
    job = JobObject()
    assert job.active
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        assert job.assign(proc.pid) is True
        assert psutil.pid_exists(proc.pid)
        # simulate the host dying: closing the last job handle reaps children
        job.kill()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.05)
        assert proc.poll() is not None, "component was orphaned — job did not reap it"
    finally:
        if proc.poll() is None:
            proc.kill()
