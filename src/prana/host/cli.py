"""CLI for prana.host — `prana host run | status | install | uninstall`.

Today: `run` and `status` (install/uninstall in Phase 4).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from prana.host import lockfile
from prana.host.component import load_components
from prana.host.log import setup_logging
from prana.host.paths import COMPONENTS_YAML, lockfile_path, log_dir
from prana.host.supervisor import Supervisor


logger = logging.getLogger("prana.host.cli")


def _cmd_run(args: argparse.Namespace) -> int:
    """Bring up all enabled components under supervision until ctrl-C."""
    setup_logging(verbose=args.verbose)
    logger.info("prana host starting")

    if not lockfile.acquire(replace=args.replace):
        sys.stderr.write(
            "host orchestrator already running. Use --replace to kill it.\n"
        )
        return 3

    try:
        components = load_components(COMPONENTS_YAML)
    except FileNotFoundError:
        sys.stderr.write(
            f"FATAL: components.yaml not found at {COMPONENTS_YAML}\n"
            f"Run `prana host install` (Phase 4) or create it from\n"
            f"prana/scripts/install/components.template.yaml.\n"
        )
        lockfile.release()
        return 4
    except (ValueError, Exception) as exc:
        sys.stderr.write(f"FATAL: components.yaml is invalid: {exc}\n")
        lockfile.release()
        return 4

    enabled = [c for c in components if c.enabled]
    logger.info("%d component(s) configured, %d enabled", len(components), len(enabled))
    for c in components:
        flag = "" if c.enabled else " [disabled]"
        logger.info("  - %s: %s%s", c.name, c.description or "(no description)", flag)

    supervisor = Supervisor(enabled)

    async def _main() -> None:
        supervisor.install_signal_handlers()
        await supervisor.run()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt during shutdown")
    finally:
        lockfile.release()

    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    """Report state of the running orchestrator + its components."""
    setup_logging(verbose=False)
    lock = lockfile.read_lock()
    if not lock:
        print("host orchestrator: NOT RUNNING")
        print(f"  (no lockfile at {lockfile_path()})")
        return 1

    pid = int(lock.get("pid", 0))
    alive = lockfile._pid_alive(pid)
    state = "RUNNING" if alive else "STALE (pid not alive)"
    print(f"host orchestrator: {state}")
    print(f"  pid:        {pid}")
    print(f"  started:    {lock.get('start_time')}")
    print(f"  argv:       {' '.join(lock.get('argv') or [])}")
    print(f"  log:        {log_dir() / 'host.log'}")
    print(f"  config:     {COMPONENTS_YAML}")
    if not alive:
        print()
        print("  Lockfile is stale. Next `prana host run` will reclaim it.")
        return 2
    print()
    print("(per-component status will appear here once the bus surfaces it — Phase 3)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="prana host",
        description="Host-level process supervisor for Narada's runtime.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="Run the supervisor in the foreground.")
    run.add_argument("-v", "--verbose", action="store_true", help="Tee logs to stderr.")
    run.add_argument("--replace", action="store_true",
                     help="Kill an existing orchestrator instance, then start.")
    run.set_defaults(func=_cmd_run)

    status = sub.add_parser("status", help="Show whether the orchestrator is running.")
    status.set_defaults(func=_cmd_status)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
