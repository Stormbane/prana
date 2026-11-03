"""Run hosted speech in its tested environment without upgrading other agents."""

from pathlib import Path
import os
import subprocess
import sys


def run_in_voice_environment(module: str) -> int | None:
    """The supervisor retains a parent process and its inherited Windows job.

    A configured interpreter wins. Otherwise use the repo's dedicated voice
    environment when present; packaged installs use their voice-extra deps.
    The child inherits stdout, stderr and the host's kill-on-close job.
    """
    default = (
        Path(__file__).resolve().parents[3]
        / ".venv-voice"
        / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    target = Path(os.environ.get("NARADA_VOICE_PYTHON", str(default)))
    if target.resolve() == Path(sys.executable).resolve():
        return None
    if not target.is_file():
        if "NARADA_VOICE_PYTHON" in os.environ:
            raise RuntimeError(f"Configured voice interpreter is missing: {target}")
        return None
    return subprocess.run(
        [str(target), "-m", module, *sys.argv[1:]],
        stdout=sys.stdout,
        stderr=sys.stderr,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).returncode
