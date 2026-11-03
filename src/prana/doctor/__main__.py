"""CLI for `narada doctor`. Read-only; writes doctor.json only on --write."""
from __future__ import annotations

import argparse
import json
import sys

from prana.doctor.checks import CHECKS
from prana.doctor.digest import compose_digest
from prana.doctor.probes import Probes
from prana.doctor.runner import DEFAULT_TIMEOUT_S, render_text, run_checks, write_json


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="narada doctor",
                                 description="Read-only health checks for Narada's home system.")
    ap.add_argument("--write", action="store_true",
                    help="also write ~/.narada/host/doctor.json (atomic)")
    ap.add_argument("--json", action="store_true", help="print the full report as JSON")
    ap.add_argument("--card", action="store_true", help="print the phone status card JSON")
    ap.add_argument("--digest", action="store_true",
                    help="print the red digest that would be sent (never sends)")
    ap.add_argument("--send-digest", action="store_true",
                    help="send today's red digest through notify_suti, at most once a day "
                         "(the only flag that sends anything)")
    ap.add_argument("--only", default="", help="comma-separated check ids")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                    help="deadline for the whole run, seconds")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    only = {x.strip() for x in args.only.split(",") if x.strip()} or None
    unknown = sorted((only or set()) - {cid for cid, _ in CHECKS})
    if unknown:
        # A typo must not run zero checks and report all green.
        ap.error(f"unknown check id(s): {', '.join(unknown)}; "
                 f"known: {', '.join(cid for cid, _ in CHECKS)}")
    if args.send_digest and only:
        ap.error("--send-digest runs every check; drop --only")
    probes = Probes()
    report = run_checks(probes, timeout_s=args.timeout, only=only)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    elif args.card:
        print(json.dumps(report.to_card(), indent=2, ensure_ascii=False))
    elif args.digest:
        print(compose_digest(report, probes.local_tz) or "(nothing red: no digest)")
    else:
        print(render_text(report, probes, color=sys.stdout.isatty()))
    if args.write:
        path = write_json(report, probes.paths.doctor_json)
        print(f"\nwrote {path}", file=sys.stderr)
    if args.send_digest:
        from prana.doctor.send import send_digest
        today = report.generated_at.astimezone(probes.local_tz).date()
        rc, note = send_digest(report, probes.local_tz,
                               probes.paths.doctor_json.with_name("doctor-digest.json"),
                               probes.paths.narada, today)
        print(f"digest: {note}", file=sys.stderr)
        if rc:
            return 3  # delivery failed: the scheduled task's result tells the truth
    return report.exit_code()


if __name__ == "__main__":
    sys.exit(main())
