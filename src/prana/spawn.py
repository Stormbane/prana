"""Hidden subprocess helpers.

When prana runs under the Narada_Host scheduled task, its parent is
``pythonw.exe`` -- no attached console. Any ``subprocess.run`` that
spawns a console executable (claude.exe, python.exe, git.exe) without
``CREATE_NO_WINDOW`` makes Windows allocate a fresh console window for
the child, which flashes briefly on screen.

Route every internal subprocess through ``run_hidden`` (or
``popen_hidden``) so the flag is set automatically. ``shell=True`` is
rejected by default -- on Windows it routes the command through
``cmd.exe`` and changes the parsing context, which makes safe quoting
of user-influenced arguments genuinely hard. Resolve the executable
with ``shutil.which`` and pass argv as a list instead.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, Sequence


# Windows: prevent console allocation when a console-less parent spawns
# a console child. No effect on POSIX (the constant is 0 there).
_NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _merge_creationflags(kwargs: dict[str, Any]) -> None:
    if os.name != "nt":
        return
    kwargs["creationflags"] = int(kwargs.get("creationflags", 0)) | _NO_WINDOW


def _reject_shell(kwargs: dict[str, Any], caller: str) -> None:
    if kwargs.get("shell"):
        raise ValueError(
            f"{caller} refuses shell=True -- resolve the executable "
            f"(shutil.which) and pass argv as a list."
        )


def run_hidden(
    argv: Sequence[str],
    **kwargs: Any,
) -> subprocess.CompletedProcess:
    """``subprocess.run`` with ``CREATE_NO_WINDOW`` set on Windows."""
    _reject_shell(kwargs, "run_hidden")
    _merge_creationflags(kwargs)
    return subprocess.run(argv, **kwargs)


def popen_hidden(argv: Sequence[str], **kwargs: Any) -> subprocess.Popen:
    """``subprocess.Popen`` with ``CREATE_NO_WINDOW`` set on Windows."""
    _reject_shell(kwargs, "popen_hidden")
    _merge_creationflags(kwargs)
    return subprocess.Popen(argv, **kwargs)
