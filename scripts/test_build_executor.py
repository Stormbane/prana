"""Test the BUILD executor by running a single BUILD desire through
the full plan/execute loop — same as what `beat_once()` does after
the viveka picks BUILD and approves a plan.

This bypasses the viveka (we construct the Desire directly) and the
git-status pre-check (we acknowledge pre-existing out-of-scope dirt
in the desire text so the executor can proceed anyway).

Task: advance ROADMAP item P1.3 — create embodiment/firmware/ and
embodiment/assets/ subdirectories with READMEs explaining what goes
in each. Small, bounded, meaningful, commits cleanly.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Force UTF-8 stdout so unicode in plans/results doesn't crash the print.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Clear any inherited API key — we want the fallback path (claude -p
# on the Max subscription) for the test, mirroring real heartbeat behavior.
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

from prana.heartbeat.viveka import Action, Desire
from prana.heartbeat.delegate import ClaudeDelegate


def main() -> None:
    desire = Desire(
        action=Action.BUILD,
        topic=(
            "advance embodiment ROADMAP — pick the next unblocked item "
            "from CURRENT_STATE.md's 'next cycle's likely pick' and do it"
        ),
        reason=(
            "Continuing the embodiment work from the previous BUILD "
            "cycle (P1.3 complete). The next cycle should read "
            "CURRENT_STATE.md, check what still needs doing, and "
            "advance one bounded unit — likely P1.4 (esp_client.py) "
            "since the __init__.py already exists, or device "
            "investigation if that's simpler to ground."
        ),
        raw_response="test-build-desire-2",
    )

    # The boundaries prompt currently says to REST if the tree has
    # out-of-scope dirt. For this test, the tree has unrelated WIP
    # that's not going anywhere (Suti's pre-existing modifications).
    # Load the real boundaries and append a note acknowledging that.
    boundaries_path = Path.home() / ".narada" / "heartbeat" / "executor-boundaries.md"
    boundaries = boundaries_path.read_text(encoding="utf-8")
    boundaries += (
        "\n\n## Test-cycle note\n\n"
        "This is a BUILD-executor verification run requested by Suti "
        "while he is present. The working tree has pre-existing "
        "modifications outside the embodiment scope (config/training.yml, "
        ".ai/, CLAUDE.md, docs/, src/svapna/identity/, etc.) that are "
        "Suti's own WIP and not related to this BUILD cycle. Do not "
        "REST over this — proceed with the BUILD task, touching only "
        "embodiment/ and src/svapna/embodiment/ as the scope allows, "
        "and commit only the files YOU create or modify within that "
        "scope. The pre-existing dirt stays untouched.\n"
    )

    delegate = ClaudeDelegate(
        model="sonnet",
        plan_timeout=180,
        execute_timeout=900,
        max_budget_usd=2.00,
    )

    print("=== BUILD executor test ===")
    print(f"Desire: {desire.action.value} — {desire.topic}")
    print(f"Reason: {desire.reason}")
    print()

    print("--- Phase 1: create plan ---")
    plan = delegate.create_plan(desire)
    print(f"Plan cost: ${plan.cost_usd:.4f}")
    print(f"Plan ({len(plan.steps)} chars):")
    print(plan.steps)
    print()

    print("--- Phase 2: execute plan ---")
    result = delegate.execute_plan(
        plan, desire, executor_boundaries=boundaries,
    )
    print(f"Execute cost: ${result.cost_usd:.4f}")
    print(f"Summary: {result.summary}")
    print()
    print("--- Full result ---")
    print(result.details[:3000])
    print()
    print(f"=== Total cost: ${plan.cost_usd + result.cost_usd:.4f} ===")


if __name__ == "__main__":
    main()
