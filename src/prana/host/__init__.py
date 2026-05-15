"""prana.host — host-level process supervisor.

Brings up Narada's runtime (heartbeat, chat bridge, agent gateway, body
brain) as supervised subprocesses with restart-on-crash, prefix-tagged
log capture, and a lockfile so we don't double-launch.

Architecture in docs/plans/host-orchestrator-2026-05-11.md.
Combined rollout in docs/plans/combined-rollout-2026-05-11.md.

CLI: `prana host run | status | install | uninstall`.
"""

__all__ = ["main"]


def main() -> int:
    from prana.host.cli import main as _main
    return _main()
