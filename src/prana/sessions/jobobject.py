"""Windows Job Object containment for spawned CLI sessions.

A session's CLI process (and everything it spawns — node, python, git)
is assigned to a Job Object with KILL_ON_JOB_CLOSE, so closing the job
handle reaps the whole tree. This is the guarantee the lifecycle state
machine relies on: `killed` means *gone*, not "the parent exited and
orphaned three children."

ctypes-only — no pywin32 dependency. No-ops gracefully off Windows so
tests can run anywhere.
"""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _JobObjectExtendedLimitInformation = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.POINTER(wintypes.ULONG)),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


class JobObject:
    """A kill-on-close Job Object wrapping one session's process tree.

    Off Windows this is a stub that tracks nothing; ``kill()`` then
    falls back to the caller's own process handling.
    """

    def __init__(self) -> None:
        self._handle = None
        if not _IS_WINDOWS:
            return
        handle = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _kernel32.SetInformationJobObject(
            handle,
            _JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            err = ctypes.get_last_error()
            _kernel32.CloseHandle(handle)
            raise ctypes.WinError(err)
        self._handle = handle

    @property
    def active(self) -> bool:
        return self._handle is not None

    def assign(self, pid: int) -> bool:
        """Assign a process (by pid) to the job. Returns False on failure."""
        if self._handle is None:
            return False
        proc = _kernel32.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid
        )
        if not proc:
            logger.warning(
                "JobObject.assign: OpenProcess(%d) failed: %s",
                pid,
                ctypes.get_last_error(),
            )
            return False
        try:
            ok = _kernel32.AssignProcessToJobObject(self._handle, proc)
            if not ok:
                logger.warning(
                    "JobObject.assign: AssignProcessToJobObject(%d) failed: %s",
                    pid,
                    ctypes.get_last_error(),
                )
            return bool(ok)
        finally:
            _kernel32.CloseHandle(proc)

    def kill(self) -> None:
        """Terminate every process in the job and release the handle."""
        if self._handle is None:
            return
        _kernel32.TerminateJobObject(self._handle, 1)
        _kernel32.CloseHandle(self._handle)
        self._handle = None

    def close(self) -> None:
        """Release the handle. KILL_ON_JOB_CLOSE reaps remaining processes."""
        if self._handle is None:
            return
        _kernel32.CloseHandle(self._handle)
        self._handle = None

    def __del__(self):  # last-resort reap; explicit kill()/close() preferred
        try:
            self.close()
        except Exception:
            pass
