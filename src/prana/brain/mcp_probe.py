"""Are the brain's tool servers alive? (Suti 2026-09-16: "I tried to log
weight but got an error saying the akhada server failed to connect. Why
didn't we know it was down already?")

Every MCP server the brain wires (smriti, akhada, notes, box, sessions)
is a subprocess the Claude CLI spawns per session. One that dies at
spawn used to surface only as Narada's apology in the chat. Two doors
now:

- ``probe()`` runs the MCP initialize handshake by hand against each
  configured server, the way the CLI would, and says which answered.
  The brain runs it once at startup, in the background.
- ``report()`` turns failures into one level-error entry in the feed
  per server per day (pushed to the phone) and a log line naming the
  cause. The session pool calls it when a fresh session's servers
  report failed even after one respawn.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

_INIT = json.dumps({
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "narada-brain-probe", "version": "1"}},
})


def probe_one(name: str, spec: dict, timeout: float = 20.0) -> str | None:
    """None when the server answers the handshake; otherwise why not, in
    a sentence (command missing, exited with rc and its last stderr
    line, no answer in time)."""
    cmd = [spec.get("command", ""), *spec.get("args", [])]
    env = {**os.environ, "PYTHONUTF8": "1", **(spec.get("env") or {})}
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, cwd=spec.get("cwd") or None)
    except FileNotFoundError:
        return f"command not found: {cmd[0]}"
    except OSError as exc:
        return f"could not start: {exc}"
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    answer: dict = {}

    def _read() -> None:
        for raw in proc.stdout:  # type: ignore[union-attr]
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == 1:
                answer["msg"] = msg
                return

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    try:
        proc.stdin.write((_INIT + "\n").encode("utf-8"))
        proc.stdin.flush()
    except OSError:
        pass  # it died before reading: the exit below says so
    reader.join(timeout)
    stderr_tail = ""
    try:
        proc.stdin.close()
    except OSError:
        pass
    if "msg" not in answer:
        # A dead one is reaped here (its stdout closed before its exit
        # status landed); a hung one is still running and gets killed.
        try:
            rc = proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            rc = None
        if rc is None:
            proc.kill()
        try:
            err = proc.stderr.read().decode("utf-8", "replace").strip().splitlines()
            stderr_tail = err[-1][:200] if err else ""
        except OSError:
            pass
        if rc is None:
            return f"no answer within {timeout:.0f}s" + (f" ({stderr_tail})" if stderr_tail else "")
        return f"exited rc={rc} without answering" + (f": {stderr_tail}" if stderr_tail else "")
    proc.kill()
    msg = answer["msg"]
    if "error" in msg:
        return f"initialize refused: {json.dumps(msg['error'])[:200]}"
    return None


def probe(servers: dict[str, dict], timeout: float = 20.0) -> dict[str, str]:
    """{name: why} for every server that did not answer; empty when all did."""
    if not servers:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(servers))) as pool:
        results = pool.map(lambda kv: (kv[0], probe_one(kv[0], kv[1], timeout)), servers.items())
    return {name: why for name, why in results if why is not None}


def report(failed: dict[str, str], where: str) -> None:
    """Say it where it will be seen: the log, and the feed (level error,
    one entry per server per day — every failed session must not become
    another notification). Never raises."""
    for name, why in failed.items():
        logger.error("tool server %s failed (%s): %s", name, where, why)
        try:
            from prana.brain.push import notify

            notify(level="error", title="Tools",
                   body=f"The {name} tool server failed to start ({where}): {why}",
                   source="brain-tools", category="tools",
                   event_id=f"tool-server-failed:{name}:{_dt.date.today().isoformat()}")
        except Exception:  # noqa: BLE001 — the feed must not take the brain down
            logger.warning("tool-server failure not posted to the feed", exc_info=True)


def check(servers: dict[str, dict], where: str, timeout: float = 20.0) -> dict[str, str]:
    """Probe, then report. Returns the failures."""
    failed = probe(servers, timeout)
    if failed:
        report(failed, where)
    else:
        logger.info("tool servers answered (%s): %s", where, ", ".join(sorted(servers)) or "none")
    return failed
