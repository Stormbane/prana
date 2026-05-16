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

    config_path = args.config or COMPONENTS_YAML
    try:
        from pathlib import Path
        components = load_components(Path(config_path))
    except FileNotFoundError:
        sys.stderr.write(
            f"FATAL: components.yaml not found at {config_path}\n"
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


def _resolve_install_script(name: str) -> "Path":
    """Find install.ps1 / uninstall.ps1 in the installed prana package."""
    from pathlib import Path
    here = Path(__file__).resolve()
    # src/prana/host/cli.py → repo root → scripts/install/
    candidate = here.parent.parent.parent.parent / "scripts" / "install" / name
    if not candidate.exists():
        sys.stderr.write(f"FATAL: install script not found at {candidate}\n")
        sys.stderr.write("This usually means prana is installed without -e and the scripts/ dir wasn't included.\n")
        raise SystemExit(5)
    return candidate


def _run_powershell(script_path: "Path", *args: str) -> int:
    """Invoke a .ps1 via powershell.exe with the strictest sane policy."""
    import subprocess as _sp
    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", str(script_path),
        *args,
    ]
    logger.info("running: %s", " ".join(cmd))
    return _sp.call(cmd)


def _cmd_install(args: argparse.Namespace) -> int:
    """Register the host orchestrator as a Windows scheduled task."""
    if sys.platform != "win32":
        sys.stderr.write("FATAL: install is currently Windows-only.\n")
        return 5
    script = _resolve_install_script("install.ps1")
    ps_args: list[str] = []
    if args.force:
        ps_args.append("-Force")
    if args.dry_run:
        ps_args.append("-DryRun")
    return _run_powershell(script, *ps_args)


def _cmd_uninstall(args: argparse.Namespace) -> int:
    """Remove the host orchestrator scheduled task."""
    if sys.platform != "win32":
        sys.stderr.write("FATAL: uninstall is currently Windows-only.\n")
        return 5
    script = _resolve_install_script("uninstall.ps1")
    return _run_powershell(script)


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
    run.add_argument("--config", type=str, default=None,
                     help=f"Path to components.yaml (default: {COMPONENTS_YAML}).")
    run.set_defaults(func=_cmd_run)

    status = sub.add_parser("status", help="Show whether the orchestrator is running.")
    status.set_defaults(func=_cmd_status)

    install = sub.add_parser("install", help="Register Narada Host as a Windows scheduled task (at logon).")
    install.add_argument("--force", action="store_true", help="Re-register even if task exists.")
    install.add_argument("--dry-run", action="store_true", help="Show what would happen without changing anything.")
    install.set_defaults(func=_cmd_install)

    uninstall = sub.add_parser("uninstall", help="Remove the Narada Host scheduled task.")
    uninstall.set_defaults(func=_cmd_uninstall)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
