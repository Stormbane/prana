"""Provider adapters — one per subscription CLI.

Each adapter owns: building the argv, spawning the process (hidden, in a
Job Object), parsing its output stream into normalized events, sending
follow-up input, and terminating. All provider-specific parsing lives
here and nowhere else — the stream formats are internal to each CLI and
drift between releases (expected; keep the blast radius one file).

Provider matrix (per the CCC-validated patterns, plan §6a):
- claude: ``claude -p --output-format stream-json --input-format
  stream-json`` with stdin held open — follow-ups are stream-json user
  messages; ``--resume <id>`` continues a prior conversation.
- codex:  ``codex exec --json`` one-shot; ``codex exec resume <id>`` to
  continue.
- kimi:   ``kimi --print --output-format stream-json`` one-shot for now.
  TODO(plan §6a): upgrade to ``kimi acp`` (JSON-RPC over stdio) for
  live steer once the ACP client is worth writing.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

from prana.sessions.jobobject import JobObject
from prana.spawn import popen_hidden

logger = logging.getLogger(__name__)

PROVIDERS = ("claude", "codex", "kimi")


class ProviderNotInstalled(RuntimeError):
    pass


@dataclass
class SessionEvent:
    """Normalized event from a provider stream."""

    kind: str          # 'init' | 'output' | 'result' | 'exit' | 'parse-error'
    text: str = ""     # human-readable payload, best effort
    provider_session_id: Optional[str] = None
    raw: dict = field(default_factory=dict)


def _which(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise ProviderNotInstalled(f"{name} CLI not found on PATH")
    return path


class SpawnedProcess:
    """A live CLI process: Popen + JobObject + a reader thread.

    The reader thread parses stdout lines into SessionEvents and hands
    them to ``on_event``. Never blocks the caller.
    """

    def __init__(
        self,
        argv: list[str],
        cwd: str,
        parse_line: Callable[[str], Optional[SessionEvent]],
        on_event: Callable[[SessionEvent], None],
        *,
        hold_stdin_open: bool,
    ) -> None:
        self.job = JobObject()
        self.proc = popen_hidden(
            argv,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # stderr merged into stdout: an undrained stderr pipe fills
            # and blocks the child (auth banners, update notices...).
            # Parsers already tolerate non-JSON lines.
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self.job.assign(self.proc.pid)
        self._hold_stdin_open = hold_stdin_open
        self._on_event = on_event
        self._parse_line = parse_line
        self._reader = threading.Thread(
            target=self._pump, name=f"session-reader-{self.proc.pid}", daemon=True
        )
        # NOT started here: the manager registers the session as RUNNING
        # first, then calls start() — otherwise a fast-exiting process
        # can emit 'exit' while the registry row is still SPAWNING.

    def start(self) -> None:
        """Begin pumping events. Call after the session is registered."""
        if not self._reader.is_alive():
            self._reader.start()

    @property
    def pid(self) -> int:
        return self.proc.pid

    def _pump(self) -> None:
        try:
            assert self.proc.stdout is not None
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                event = self._parse_line(line)
                if event is not None:
                    self._on_event(event)
        except Exception as exc:  # reader must never take the manager down
            logger.warning("reader %d died: %s", self.proc.pid, exc)
        finally:
            code = self.proc.wait()
            self._on_event(SessionEvent(kind="exit", text=str(code),
                                        raw={"exit_code": code}))
            self.job.close()

    def send_line(self, line: str) -> None:
        if self.proc.stdin is None or self.proc.poll() is not None:
            raise RuntimeError("process is not accepting input")
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def close_stdin(self) -> None:
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass

    def kill(self) -> None:
        """Reap the whole tree via the job; fall back to Popen.kill."""
        self.job.kill()
        if self.proc.poll() is None:
            self.proc.kill()


# ── claude ───────────────────────────────────────────────────────────


def _parse_claude_line(line: str) -> Optional[SessionEvent]:
    try:
        obj = json.loads(line)
    except ValueError:
        return SessionEvent(kind="parse-error", text=line[:200])
    kind = obj.get("type", "")
    if kind == "system" and obj.get("subtype") == "init":
        return SessionEvent(
            kind="init",
            provider_session_id=obj.get("session_id"),
            raw=obj,
        )
    if kind == "assistant":
        content = obj.get("message", {}).get("content", [])
        text = " ".join(
            b.get("text", "") for b in content if isinstance(b, dict)
            and b.get("type") == "text"
        ).strip()
        return SessionEvent(kind="output", text=text, raw=obj)
    if kind == "result":
        return SessionEvent(
            kind="result",
            text=str(obj.get("result", ""))[:2000],
            provider_session_id=obj.get("session_id"),
            raw=obj,
        )
    return SessionEvent(kind="output", raw=obj)


def spawn_claude(
    prompt: str,
    cwd: str,
    on_event: Callable[[SessionEvent], None],
    *,
    resume_session_id: Optional[str] = None,
) -> SpawnedProcess:
    argv = [
        _which("claude"), "-p",
        "--output-format", "stream-json",
        "--input-format", "stream-json",
        "--verbose",
    ]
    if resume_session_id:
        argv += ["--resume", resume_session_id]
    sp = SpawnedProcess(
        argv, cwd, _parse_claude_line, on_event, hold_stdin_open=True
    )
    sp.send_line(json.dumps({
        "type": "user",
        "message": {"role": "user",
                    "content": [{"type": "text", "text": prompt}]},
    }))
    return sp


def send_claude_followup(sp: SpawnedProcess, text: str) -> None:
    sp.send_line(json.dumps({
        "type": "user",
        "message": {"role": "user",
                    "content": [{"type": "text", "text": text}]},
    }))


# ── codex ────────────────────────────────────────────────────────────


def _parse_codex_line(line: str) -> Optional[SessionEvent]:
    try:
        obj = json.loads(line)
    except ValueError:
        # codex exec mixes human text with JSON depending on flags
        return SessionEvent(kind="output", text=line[:500])
    sid = obj.get("session_id") or obj.get("thread_id")
    kind = obj.get("type", "")
    if sid and kind in ("session.created", "thread.started"):
        return SessionEvent(kind="init", provider_session_id=str(sid), raw=obj)
    text = obj.get("text") or obj.get("message") or ""
    if isinstance(text, dict):
        text = json.dumps(text)[:500]
    return SessionEvent(kind="output", text=str(text)[:2000], raw=obj)


def spawn_codex(
    prompt: str,
    cwd: str,
    on_event: Callable[[SessionEvent], None],
    *,
    resume_session_id: Optional[str] = None,
) -> SpawnedProcess:
    argv = [_which("codex"), "exec", "--json"]
    if resume_session_id:
        argv += ["resume", resume_session_id]
    argv.append(prompt)
    sp = SpawnedProcess(
        argv, cwd, _parse_codex_line, on_event, hold_stdin_open=False
    )
    sp.close_stdin()
    return sp


# ── kimi ─────────────────────────────────────────────────────────────


def _parse_kimi_line(line: str) -> Optional[SessionEvent]:
    try:
        obj = json.loads(line)
    except ValueError:
        return SessionEvent(kind="output", text=line[:500])
    sid = obj.get("session_id")
    if sid and obj.get("type") in ("init", "session"):
        return SessionEvent(kind="init", provider_session_id=str(sid), raw=obj)
    text = obj.get("text") or obj.get("content") or ""
    if isinstance(text, (dict, list)):
        text = json.dumps(text)[:500]
    return SessionEvent(kind="output", text=str(text)[:2000], raw=obj)


def spawn_kimi(
    prompt: str,
    cwd: str,
    on_event: Callable[[SessionEvent], None],
    *,
    resume_session_id: Optional[str] = None,  # TODO: kimi acp live steer
) -> SpawnedProcess:
    argv = [
        _which("kimi"), "--print", "--output-format", "stream-json", "-p", prompt,
    ]
    sp = SpawnedProcess(
        argv, cwd, _parse_kimi_line, on_event, hold_stdin_open=False
    )
    sp.close_stdin()
    return sp


SPAWNERS = {
    "claude": spawn_claude,
    "codex": spawn_codex,
    "kimi": spawn_kimi,
}
