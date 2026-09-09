"""The Codex backend — OpenAI models on the ChatGPT subscription, the
way the Claude backend uses the Claude Code subscription (Suti
2026-09-09: "add OpenAI models via the Codex SDK as a model option,
the same way we use Claude Code").

One ``codex exec --json`` process per turn; the thread id is the
native session id and later turns ``resume`` it, so Codex keeps the
conversation and its prompt cache. ``--approve-for-me`` routes MCP
tool approvals through Codex's automatic review with the sandbox kept
(workspace-write, scoped to the session directory) — the phone chat
still cannot touch a repo. Instructions travel as AGENTS.md in that
directory: Codex reads no system prompt from the command line.

Images: Codex's built-in imagegen skill writes PNGs under
``<codex home>/generated_images/<thread id>/``; after the turn every
new one is copied into the brain's image store and yielded as an image
card, so Narada can draw and the app shows it (Suti: "make sure Narada
can generate images and display them").

Probed on codex-cli 0.153.4 (2026-09-09): parent flags before the
``resume`` subcommand carry through; ``resume`` without
``--approve-for-me`` falls back to approval-never and MCP calls fail.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

from prana.brain.cards import CARD_V, ActivityEvent, CardEvent, ToolTracker

logger = logging.getLogger(__name__)

CODEX_NOTES = """
## Working as Narada from Codex

This directory is your session's workspace; the sandbox lets you write
here and nowhere else. Your tools — akhada (fitness), smriti (memory),
notes, the house — are MCP servers: call them directly, approvals are
automatic. Do not read or explore repositories; you are a chat, not a
coding session (see above).

You can generate images (the imagegen skill). Every image you generate
during a turn is picked up afterwards and shown in the app on its own
— describe what you drew in a line and stop there; do not copy, move,
open or link the file yourself.
"""

# Turn-level noise Codex reports as error items.
_IGNORED_ERRORS = ("clamping SessionEnd hook timeout",)

# One JSONL event can carry a whole MCP result or command output; the
# asyncio default (64 KiB) aborted legitimate turns (review).
LINE_LIMIT = 16 * 1024 * 1024


async def kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Terminate ``proc`` and its descendants."""
    if sys.platform == "win32":
        def _taskkill() -> None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, check=False)
        await asyncio.to_thread(_taskkill)
    else:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def codex_binary() -> list[str]:
    """The command prefix for Codex: an explicit path from the
    environment, else whatever is on PATH. A .cmd shim needs cmd.exe on
    Windows (asyncio cannot exec it directly)."""
    explicit = os.environ.get("NARADA_CODEX_BIN")
    found = explicit or shutil.which("codex") or "codex"
    if found.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", found]
    return [found]


def _toml_str(value: str) -> str:
    """A TOML basic string (json's escaping is TOML-compatible for the
    paths and tokens we pass)."""
    return json.dumps(str(value), ensure_ascii=False)


# Codex features a brain session must not have (recheck): "apps" is
# Codex's bridge to the user's Google accounts (Gmail, Drive, Calendar…)
# and would have been Narada's too; the browser and computer-use
# bridges and sub-agent spawning likewise. Disabled per launch, so no
# host setting can turn them back on.
DISABLED_FEATURES: tuple[str, ...] = ("apps", "browser_use", "computer_use", "multi_agent")


def mcp_overrides(mcp_servers: dict) -> list[str]:
    """``-c`` overrides that give Codex the brain's own MCP wiring —
    the tier's servers, each with the brain's interpreter and args.
    The user's config launches akhada with bare "python", which under
    the supervisor is a venv without it: the first live turn (2026-09-09)
    reported "akhada's tools aren't available". Same lesson as
    config._mcp_servers, applied to Codex."""
    out: list[str] = []
    for name, spec in sorted(mcp_servers.items()):
        if not isinstance(spec, dict) or not spec.get("command"):
            continue
        out += ["-c", f"mcp_servers.{name}.enabled=true"]
        out += ["-c", f"mcp_servers.{name}.command={_toml_str(spec['command'])}"]
        args = spec.get("args") or []
        out += ["-c", f"mcp_servers.{name}.args=[{', '.join(_toml_str(a) for a in args)}]"]
        for key, value in (spec.get("env") or {}).items():
            out += ["-c", f"mcp_servers.{name}.env.{key}={_toml_str(value)}"]
    return out


def build_command(*, model: str, reasoning: str, disabled_servers: tuple[str, ...],
                  images: list[str], resume: str | None,
                  mcp_servers: dict | None = None) -> list[str]:
    """The argv for one turn. The prompt travels on stdin, never argv
    (a .cmd shim truncates an argument at its first newline — the
    morning-message lesson, 2026-09-08). Flags sit BEFORE ``resume``:
    the subcommand accepts only a subset and the parent's carry through.

    A controlled configuration (recheck): the user's config.toml is
    ignored outright — no host MCP servers, no plugins, nothing that
    can change between two turns — and the tier's servers are declared
    here with the brain's command, args and env. Auth is separate from
    config and stays."""
    cmd = codex_binary() + [
        "exec", "--json", "--skip-git-repo-check", "--ignore-user-config", "--approve-for-me",
        "-m", model,
        "-c", f'model_reasoning_effort="{reasoning}"',
    ]
    for feature in DISABLED_FEATURES:
        cmd += ["--disable", feature]
    tier = set((mcp_servers or {}).keys())
    for server in sorted(set(disabled_servers) - tier):
        cmd += ["-c", f"mcp_servers.{server}.enabled=false"]
    cmd += mcp_overrides(mcp_servers or {})
    for path in images:
        cmd += ["-i", path]
    if resume:
        cmd += ["resume", resume]
    return cmd


def _result_text(result: Any) -> str:
    """An MCP result's text: Codex wraps it as {content: [{type: text,
    text}]}; anything else is stringified."""
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            return "".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text")
        return json.dumps(result)
    return result if isinstance(result, str) else ""


class TurnReader:
    """Turns Codex's JSONL events into the brain's stream: text, chips
    (via ToolTracker) and cards. Pure over parsed events so the wire
    can be tested from recorded lines."""

    def __init__(self) -> None:
        self.thread_id: str | None = None
        self.completed = False
        self.failed: str | None = None
        self.errors: list[str] = []
        self._tracker = ToolTracker()
        self._said_something = False

    def feed(self, event: dict) -> list[Any]:
        """Events for one parsed line: str (text), ActivityEvent, CardEvent."""
        kind = event.get("type")
        if kind == "thread.started":
            tid = event.get("thread_id")
            if isinstance(tid, str) and tid:
                self.thread_id = tid
            return []
        if kind == "turn.completed":
            self.completed = True
            return []
        if kind == "turn.failed":
            err = event.get("error")
            self.failed = (err.get("message") if isinstance(err, dict) else str(err)) or "turn failed"
            return []
        if kind not in ("item.started", "item.completed"):
            return []
        item = event.get("item")
        if not isinstance(item, dict):
            return []
        item_type = item.get("type")
        item_id = str(item.get("id") or "")
        done = kind == "item.completed"
        if item_type == "agent_message" and done:
            text = item.get("text")
            if not isinstance(text, str) or text == "":
                return []
            out = ("\n\n" if self._said_something else "") + text
            self._said_something = True
            return [out]
        if item_type == "mcp_tool_call":
            name = f"mcp__{item.get('server', 'tool')}__{item.get('tool', 'call')}"
            if not done:
                return list(self._tracker.on_use(item_id, name, item.get("arguments")))
            failed = item.get("status") == "failed" or bool(item.get("error"))
            text = _result_text(item.get("result"))
            if failed and not text:
                err = item.get("error")
                text = json.dumps({"error": err.get("message") if isinstance(err, dict) else str(err)})
            return list(self._tracker.on_result(item_id, text, failed))
        if item_type == "command_execution":
            if not done:
                return list(self._tracker.on_use(item_id, "Bash", {"command": item.get("command")}))
            code = item.get("exit_code")
            failed = item.get("status") == "failed" or (isinstance(code, int) and code != 0)
            return list(self._tracker.on_result(
                item_id, str(item.get("aggregated_output") or ""), failed))
        if item_type == "web_search" and done:
            events = list(self._tracker.on_use(item_id, "WebSearch", {"query": item.get("query")}))
            events += self._tracker.on_result(item_id, "")
            return events
        if item_type == "error" and done:
            message = str(item.get("message") or "")
            if message and not message.startswith(_IGNORED_ERRORS):
                self.errors.append(message)
            return []
        return []


def harvest_images(codex_home: Path, thread_id: str | None, since: float,
                   images_dir: Path) -> list[CardEvent]:
    """Copy every image Codex generated for this thread since ``since``
    into the brain's store; one image card each. Never raises."""
    if not thread_id:
        return []
    cards: list[CardEvent] = []
    try:
        folder = codex_home / "generated_images" / thread_id
        if not folder.is_dir():
            return []
        for src in sorted(folder.iterdir(), key=lambda p: p.stat().st_mtime):
            if src.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
                continue
            if src.stat().st_mtime < since - 2:
                continue
            images_dir.mkdir(parents=True, exist_ok=True)
            name = f"{uuid.uuid4().hex[:12]}{src.suffix.lower()}"
            shutil.copyfile(src, images_dir / name)
            cards.append(CardEvent({
                "card_id": f"img-{name.split('.')[0]}", "kind": "image", "v": CARD_V,
                "as_of": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "caption": "Narada drew this",
                "data": {"url": f"/api/images/{name}", "alt": "an image Narada generated"},
            }))
    except OSError:
        logger.warning("image harvest failed", exc_info=True)
    return cards


class CodexBackend:
    def __init__(
        self,
        *,
        model: str,
        system_append: str,
        mcp_servers: dict,
        max_tool_iterations: int,
        cwd: str,
        resume: str | None = None,
        disallowed_tools: list[str] | None = None,
        handover: str | None = None,
        codex_home: Path | None = None,
        images_dir: Path | None = None,
        reasoning: str = "medium",
        disabled_servers: tuple[str, ...] = (),
    ):
        del max_tool_iterations, disallowed_tools  # Codex's own config rules these
        self._mcp_servers = dict(mcp_servers or {})
        self._model = model
        self._system_append = system_append
        # Codex's writable workspace is a scratch subdirectory, NOT the
        # session directory: turns.json, transcript.jsonl and the pins
        # live one level up, outside workspace-write's reach (review).
        self._cwd = Path(cwd) / "codex"
        self._native_session_id: str | None = resume
        self._handover = handover
        self._codex_home = codex_home or Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        self._images_dir = images_dir or (Path.home() / ".narada" / "brain" / "images")
        self._reasoning = reasoning
        self._disabled = tuple(disabled_servers)
        self._proc: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        self._cwd.mkdir(parents=True, exist_ok=True)
        (self._cwd / "AGENTS.md").write_text(
            self._system_append.rstrip() + "\n" + CODEX_NOTES, encoding="utf-8")

    def _materialize(self, prompt: Any) -> tuple[str, list[str]]:
        """A plain prompt, or Claude-style content blocks: text plus
        base64 images written to files for ``-i``."""
        if isinstance(prompt, str):
            return prompt, []
        texts: list[str] = []
        images: list[str] = []
        inbox = self._cwd / "inbox"
        for block in prompt if isinstance(prompt, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                texts.append(str(block.get("text", "")))
            elif block.get("type") == "image":
                src = block.get("source") or {}
                data = src.get("data") if isinstance(src, dict) else None
                if not isinstance(data, str):
                    continue
                media = str(src.get("media_type", "image/png"))
                ext = {"image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}.get(media, ".png")
                inbox.mkdir(parents=True, exist_ok=True)
                path = inbox / f"{uuid.uuid4().hex[:10]}{ext}"
                try:
                    path.write_bytes(base64.b64decode(data))
                except (ValueError, OSError):
                    continue
                images.append(str(path))
        return "\n".join(texts), images

    async def run_turn(self, prompt) -> AsyncIterator[Any]:
        text, images = self._materialize(prompt)
        # The handover rides the first turn and is forgotten only once
        # that turn COMPLETES (review: a failed first turn dropped it).
        if self._handover:
            text = self._handover.rstrip() + "\n\n" + text
        cmd = build_command(model=self._model, reasoning=self._reasoning,
                            disabled_servers=self._disabled, images=images,
                            resume=self._native_session_id, mcp_servers=self._mcp_servers)
        started = time.time()
        reader = TurnReader()
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(self._cwd),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=LINE_LIMIT)
        except OSError as exc:
            raise RuntimeError(f"codex could not be started: {exc}") from exc
        proc = self._proc
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

        async def feed_stdin() -> None:
            # Concurrent with reading: a prompt larger than the pipe
            # would otherwise deadlock against unread stdout.
            try:
                proc.stdin.write(text.encode("utf-8"))
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                proc.stdin.close()

        stdin_task = asyncio.create_task(feed_stdin())
        stderr_task = asyncio.create_task(proc.stderr.read())
        stderr = ""
        try:
            while True:
                try:
                    raw = await proc.stdout.readline()
                except ValueError as exc:  # a line past LINE_LIMIT
                    raise RuntimeError(f"codex event too large: {exc}") from exc
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("{"):
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                for out in reader.feed(event):
                    yield out
                if reader.thread_id and reader.thread_id != self._native_session_id:
                    self._native_session_id = reader.thread_id
            await proc.wait()
            stderr = (await stderr_task).decode("utf-8", errors="replace")
        finally:
            await self._end(proc)
            for task in (stdin_task, stderr_task):
                if not task.done():
                    task.cancel()
            self._proc = None
        if reader.failed:
            raise RuntimeError(f"codex turn failed: {reader.failed}")
        if not reader.completed:
            tail = "; ".join(reader.errors[-2:]) or stderr.strip().splitlines()[-1:] or ["no turn.completed"]
            raise RuntimeError(f"codex exited {proc.returncode} before completing the turn: {tail}")
        self._handover = None
        for card in harvest_images(self._codex_home, reader.thread_id, started, self._images_dir):
            yield card

    async def _end(self, proc: asyncio.subprocess.Process) -> None:
        """Make sure the whole process tree is gone and reaped. On
        Windows the shim (cmd.exe) is what we hold; killing it alone
        leaves codex.exe running with our pipes (review) — taskkill /T
        takes the tree."""
        if proc.returncode is None:
            await kill_tree(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            logger.warning("codex process %s did not exit after kill", proc.pid)

    async def cancel(self) -> None:
        proc = self._proc
        if proc is not None and proc.returncode is None:
            await self._end(proc)

    async def close(self) -> None:
        await self.cancel()

    @property
    def native_session_id(self) -> str | None:
        return self._native_session_id


__all__ = ["CodexBackend", "TurnReader", "build_command", "harvest_images", "ActivityEvent"]
