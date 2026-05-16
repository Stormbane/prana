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
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from prana.host.component import Component
from prana.host.log import component_logger


SHUTDOWN_GRACE_S = 5.0
RATE_LIMIT_WINDOW_S = 60.0
RATE_LIMIT_MAX_EXITS = 3
RATE_LIMIT_COOLDOWN_S = 300.0  # 5 min

# Health check tunables
HEALTH_PROBE_TIMEOUT_S = 3.0
HEALTH_FAILURES_TO_RESTART = 3

# wait_for_url gate
WAIT_FOR_URL_TIMEOUT_S = 1.5
WAIT_FOR_URL_POLL_INTERVAL_S = 2.0
WAIT_FOR_URL_MAX_S = 300.0  # 5 min — refuse to wait longer than this


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
    # Health state (Phase 3)
    health_consecutive_failures: int = 0
    last_health_at: Optional[float] = None
    last_health_ok: Optional[bool] = None
    waiting_for_dependency: Optional[str] = None  # url being waited on, if any

    @property
    def status(self) -> str:
        now = time.time()
        if self.waiting_for_dependency:
            return "waiting-for-dependency"
        if self.cooled_down_until and now < self.cooled_down_until:
            return "cooled-down"
        if self.pid:
            if self.last_health_ok is False:
                return "running-but-unhealthy"
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

    async def _wait_for_dependency(self) -> bool:
        """If component declares wait_for_url, poll until reachable.

        Returns True when ready (or no dependency declared). Returns
        False if the deadline elapses; caller should treat that as a
        spawn failure (will retry after restart_grace_s).
        """
        url = self.component.wait_for_url
        if not url:
            return True
        self.state.waiting_for_dependency = url
        self.logger.info("waiting for dependency: %s", url)
        deadline = time.time() + WAIT_FOR_URL_MAX_S
        while time.time() < deadline:
            if await asyncio.to_thread(self._http_probe, url):
                self.state.waiting_for_dependency = None
                self.logger.info("dependency ready: %s", url)
                return True
            await asyncio.sleep(WAIT_FOR_URL_POLL_INTERVAL_S)
        self.state.waiting_for_dependency = None
        self.logger.error(
            "dependency %s did not become ready within %ds — abandoning spawn",
            url, WAIT_FOR_URL_MAX_S,
        )
        return False

    @staticmethod
    def _http_probe(url: str) -> bool:
        """Synchronous HTTP HEAD/GET probe. True on 2xx, False otherwise."""
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=WAIT_FOR_URL_TIMEOUT_S) as r:
                return 200 <= r.status < 300
        except (urllib.error.URLError, OSError, TimeoutError, ValueError):
            return False

    async def _health_loop(self) -> None:
        """Periodic health check while the process is running.

        Three consecutive failures → terminate the process (the main
        wait loop will see it exit and respawn after restart_grace_s).
        """
        url = self.component.health_url
        if not url:
            return
        while self._proc and self._proc.returncode is None:
            await asyncio.sleep(self.component.health_interval_s)
            if not self._proc or self._proc.returncode is not None:
                return
            ok = await asyncio.to_thread(self._http_probe, url)
            self.state.last_health_at = time.time()
            self.state.last_health_ok = ok
            if ok:
                if self.state.health_consecutive_failures:
                    self.logger.info("health recovered")
                self.state.health_consecutive_failures = 0
            else:
                self.state.health_consecutive_failures += 1
                self.logger.warning(
                    "health probe failed (%d/%d): %s",
                    self.state.health_consecutive_failures,
                    HEALTH_FAILURES_TO_RESTART,
                    url,
                )
                if self.state.health_consecutive_failures >= HEALTH_FAILURES_TO_RESTART:
                    self.logger.error(
                        "%d consecutive health failures — terminating for restart",
                        HEALTH_FAILURES_TO_RESTART,
                    )
                    await self._terminate()
                    return

    async def _spawn_and_wait(self) -> None:
        """Spawn one instance of the subprocess and wait for it to exit."""
        c = self.component
        # cwd may not yet exist for some setups — fail loud rather than silent
        if not c.cwd.exists():
            raise FileNotFoundError(f"cwd does not exist: {c.cwd}")

        # Wait for any declared dependency
        if not await self._wait_for_dependency():
            return  # caller will retry after restart_grace_s

        # Reset health counters on each fresh spawn
        self.state.health_consecutive_failures = 0
        self.state.last_health_ok = None

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
        # Health checks run alongside if a URL was configured
        health_task = (
            asyncio.create_task(self._health_loop(), name=f"{c.name}-health")
            if c.health_url else None
        )

        try:
            rc = await self._proc.wait()
        finally:
            # Stop the health loop if running
            if health_task and not health_task.done():
                health_task.cancel()
            # Drain remaining buffered output
            tasks = [t for t in [pump_out, pump_err, health_task] if t]
            await asyncio.gather(*tasks, return_exceptions=True)

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
                "health_url": r.component.health_url,
                "last_health_ok": r.state.last_health_ok,
                "health_consecutive_failures": r.state.health_consecutive_failures,
                "waiting_for_dependency": r.state.waiting_for_dependency,
            }
            for name, r in self.runners.items()
        }
