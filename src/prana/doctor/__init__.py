"""narada doctor: read-only stability checks over Narada's home system.

    python -m prana.doctor            human summary (exit 0 green, 1 amber, 2 red)
    python -m prana.doctor --write    also write ~/.narada/host/doctor.json
    python -m prana.doctor --json     print the full report JSON
    python -m prana.doctor --card     print the phone status card JSON
    python -m prana.doctor --digest   print the red digest that WOULD be sent

Nothing here restarts, reconfigures or messages anything. See
docs/doctor.md for the JSON shapes, the digest and the external
dead-man's switch designs.
"""
from prana.doctor.model import AMBER, GREEN, RED, CheckResult, Report
from prana.doctor.probes import Paths, Probes
from prana.doctor.runner import render_text, run_checks, write_json

__all__ = ["AMBER", "GREEN", "RED", "CheckResult", "Paths", "Probes", "Report",
           "render_text", "run_checks", "write_json"]
