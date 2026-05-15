"""Async supervisor — one task per component, restart on exit.

Each ComponentRunner owns:
  - The subprocess (spawn, monitor, terminate)
  - Pipe pumps (stdout + stderr → unified host log with [name] prefix)
  - Restart loop with rate limiter (3 exits / 60s → cool down 5 min)
  - Phase-3-deferred: health URL polling

Supervisor coordinates:
  - Spawning all enabled components on startup
  - Trapping SIGINT/SIGTERM and shutting children down gracefully
  - Surfacing per-component state to a future `prana host status`
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from prana.host.component import Component
from prana.host.log import component_logger


SHUTDOWN_GRACE_S = 5.0
RATE_LIMIT_WINDOW_S = 60.0
RATE_LIMIT_MAX_EXITS = 3
RATE_LIMIT_COOLDOWN_S = 300.0  # 5 min


@dataclass
class ComponentState:
    """Live state of a supervised component, queryable from status."""
    component: Component
    pid: Optional[int] = None
    started_at: Optional[float] = None
    last_exit_at: Optional[float] = None
    last_exit_code: Optional[int] = None
    restart_count: int = 0
    cooled_down_until: Optional[float] = None  # epoch; None if running normally
    recent_exits: deque = field(default_factory=lambda: deque(maxlen=RATE_LIMIT_MAX_EXITS))

    @property
    def status(self) -> str:
        now = time.time()
        if self.cooled_down_until and now < self.cooled_down_until:
            return "cooled-down"
        if self.pid:
            return "running"
        if self.last_exit_code is not None:
            return "restarting"
        return "starting"


class ComponentRunner:
    """Per-component lifecycle: spawn → log → wait-exit → restart loop."""

    def __init__(self, component: Component):
        self.component = component
        self.state = ComponentState(component=component)
        self.logger = component_logger(component.name)
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._stop_requested = False

    async def run(self) -> None:
        """Restart loop. Returns when supervisor signals shutdown."""
        if not self.component.enabled:
            self.logger.info("disabled — not spawning")
            return

        while not self._stop_requested:
            # Rate-limit check
            now = time.time()
            if self.state.cooled_down_until and now < self.state.cooled_down_until:
                wait = self.state.cooled_down_until - now
                self.logger.warning(
                    "cooled down — %d more seconds before retry",
                    int(wait),
                )
                await asyncio.sleep(min(wait, 30.0))
                continue

            try:
                await self._spawn_and_wait()
            except asyncio.CancelledError:
                self.logger.info("cancelled — terminating")
                await self._terminate()
                raise
            except Exception as exc:
                self.logger.exception("spawn/wait raised: %s", exc)

            if self._stop_requested:
                break

            # Record exit, check rate limit
            self.state.recent_exits.append(time.time())
            self._check_rate_limit()

            grace = self.component.restart_grace_s
            if grace > 0:
                self.logger.info("waiting %.1fs before restart", grace)
                await asyncio.sleep(grace)

    def _check_rate_limit(self) -> None:
        """If 3 exits in 60s, cool down for 5 min."""
        if len(self.state.recent_exits) < RATE_LIMIT_MAX_EXITS:
            return
        oldest = self.state.recent_exits[0]
        if (time.time() - oldest) > RATE_LIMIT_WINDOW_S:
            return
        self.state.cooled_down_until = time.time() + RATE_LIMIT_COOLDOWN_S
        self.logger.error(
            "%d exits in %.0fs — cooling down %ds",
            len(self.state.recent_exits),
            RATE_LIMIT_WINDOW_S,
            RATE_LIMIT_COOLDOWN_S,
        )

    async def _spawn_and_wait(self) -> None:
        """Spawn one instance of the subprocess and wait for it to exit."""
        c = self.component
        # cwd may not yet exist for some setups — fail loud rather than silent
        if not c.cwd.exists():
            raise FileNotFoundError(f"cwd does not exist: {c.cwd}")

        self.logger.info("spawning: %s", " ".join(c.command))
        self.logger.debug("  cwd=%s", c.cwd)

        self._proc = await asyncio.create_subprocess_exec(
            *c.command,
            cwd=str(c.cwd),
            env=c.spawn_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Windows: CREATE_NO_WINDOW so subprocess doesn't pop a console
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        self.state.pid = self._proc.pid
        self.state.started_at = time.time()
        self.logger.info("spawned pid=%d", self._proc.pid)

        # Start pipe pumps — these run until the process closes its streams
        pump_out = asyncio.create_task(
            self._pump(self._proc.stdout, level=logging.INFO, tag="out"),
            name=f"{c.name}-stdout",
        )
        pump_err = asyncio.create_task(
            self._pump(self._proc.stderr, level=logging.WARNING, tag="err"),
            name=f"{c.name}-stderr",
        )

        try:
            rc = await self._proc.wait()
        finally:
            # Drain remaining buffered output
            await asyncio.gather(pump_out, pump_err, return_exceptions=True)

        self.state.pid = None
        self.state.last_exit_at = time.time()
        self.state.last_exit_code = rc
        self.state.restart_count += 1
        level = logging.INFO if rc == 0 else logging.WARNING
        self.logger.log(level, "exited rc=%d (restart_count=%d)", rc, self.state.restart_count)

    async def _pump(self, stream: Optional[asyncio.StreamReader], *, level: int, tag: str) -> None:
        """Forward child stream to the component logger line-by-line."""
        if stream is None:
            return
        while True:
            try:
                line = await stream.readline()
            except (ValueError, asyncio.IncompleteReadError):
                # Buffer overflow on a single line — try smaller read
                line = await stream.read(8192)
                if not line:
                    break
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                self.logger.log(level, "%s: %s", tag, text)

    async def _terminate(self) -> None:
        """SIGTERM then SIGKILL after grace."""
        if not self._proc or self._proc.returncode is not None:
            return
        try:
            self._proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=SHUTDOWN_GRACE_S)
        except asyncio.TimeoutError:
            self.logger.warning("did not exit within %.1fs of SIGTERM — killing", SHUTDOWN_GRACE_S)
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                self.logger.error("did not die after SIGKILL")

    def request_stop(self) -> None:
        self._stop_requested = True


class Supervisor:
    """Orchestrates ComponentRunners. One asyncio task per component."""

    def __init__(self, components: list[Component]):
        self.components = components
        self.runners: dict[str, ComponentRunner] = {
            c.name: ComponentRunner(c) for c in components
        }
        self._tasks: dict[str, asyncio.Task] = {}
        self._shutdown_event = asyncio.Event()
        self._logger = logging.getLogger("prana.host.supervisor")

    async def run(self) -> None:
        """Spawn all runners; return when shutdown is requested."""
        if not self.runners:
            self._logger.warning("no components configured — nothing to do")
            return

        for name, runner in self.runners.items():
            self._tasks[name] = asyncio.create_task(runner.run(), name=f"runner:{name}")

        self._logger.info("supervisor up — %d component(s)", len(self.runners))

        await self._shutdown_event.wait()

        self._logger.info("shutdown — stopping %d component(s)", len(self.runners))
        for runner in self.runners.values():
            runner.request_stop()
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._logger.info("shutdown complete")

    def install_signal_handlers(self) -> None:
        """Trap Ctrl-C / SIGTERM and trigger graceful shutdown."""
        loop = asyncio.get_running_loop()

        def _on_signal(signame: str) -> None:
            self._logger.info("caught %s — shutting down", signame)
            self._shutdown_event.set()

        if os.name == "posix":
            for s in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(s, _on_signal, s.name)
        else:
            # Windows asyncio doesn't support add_signal_handler for SIGTERM;
            # SIGINT works via the default Ctrl-C delivery. Wire a thread that
            # listens for SIGBREAK as a fallback.
            def _win_handler(signum, frame):
                self._logger.info("caught signal %d — shutting down", signum)
                # set the event from sync land — loop.call_soon_threadsafe
                asyncio.get_event_loop().call_soon_threadsafe(self._shutdown_event.set)
            try:
                signal.signal(signal.SIGINT, _win_handler)
                signal.signal(signal.SIGBREAK, _win_handler)  # type: ignore[attr-defined]
            except (AttributeError, ValueError):
                pass

    def snapshot(self) -> dict:
        """Status snapshot for `prana host status`."""
        return {
            name: {
                "status": r.state.status,
                "pid": r.state.pid,
                "restart_count": r.state.restart_count,
                "last_exit_code": r.state.last_exit_code,
            }
            for name, r in self.runners.items()
        }
