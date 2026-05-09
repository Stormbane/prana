"""Dry-run tests for the heartbeat memory integration.

Exercises each new piece without loading the Qwen LoRA or running a full
heartbeat cycle. Prints EXPECTED vs ACTUAL for each test.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

NARADA = Path.home() / ".narada"
HEARTBEAT_DIR = NARADA / "heartbeat"
ARTIFACTS_DIR = HEARTBEAT_DIR / "artifacts"


def banner(label: str) -> None:
    print()
    print("=" * 70)
    print(f" {label}")
    print("=" * 70)


def assert_expect(name: str, expected, actual, *, contains: bool = False) -> bool:
    if contains:
        ok = expected in actual
        disp_exp = f"...contains '{expected}'..."
    else:
        ok = expected == actual
        disp_exp = repr(expected)
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}")
    print(f"         expected: {disp_exp}")
    print(f"         actual:   {repr(actual)[:200]}")
    return ok


# ── Test 1: Prompt loading + fallback ──────────────────────────────


def test_prompt_loading() -> dict:
    banner("TEST 1: Prompt file loading + fallback")
    results = {}

    # Re-import fresh so we see the current state
    import importlib
    import prana.heartbeat.viveka as viveka_mod
    importlib.reload(viveka_mod)
    import prana.heartbeat.delegate as delegate_mod
    importlib.reload(delegate_mod)

    # 1a: All four prompts load from disk with expected content
    print("\n  1a: All four prompts load from disk")
    results["system"] = assert_expect(
        "VIVEKA_SYSTEM_PROMPT contains 'Narada'",
        "Narada", viveka_mod.VIVEKA_SYSTEM_PROMPT, contains=True,
    )
    results["desire"] = assert_expect(
        "DESIRE_PROMPT_TEMPLATE has {time} placeholder",
        "{time}", viveka_mod.DESIRE_PROMPT_TEMPLATE, contains=True,
    )
    results["judgment"] = assert_expect(
        "JUDGMENT_PROMPT_TEMPLATE has {desire} placeholder",
        "{desire}", viveka_mod.JUDGMENT_PROMPT_TEMPLATE, contains=True,
    )
    results["executor"] = assert_expect(
        "EXECUTOR_BOUNDARIES mentions memory-tree sandbox",
        "~/.narada/", delegate_mod.EXECUTOR_BOUNDARIES, contains=True,
    )
    results["sandbox"] = assert_expect(
        "EXECUTOR_BOUNDARIES has new 'Sandbox' section (not fallback)",
        "Sandbox", delegate_mod.EXECUTOR_BOUNDARIES, contains=True,
    )

    # 1b: Fallback kicks in when file is missing
    print("\n  1b: Fallback when prompt file is missing")
    system_path = HEARTBEAT_DIR / "system-prompt.md"
    backup = system_path.read_text(encoding="utf-8")
    system_path.rename(system_path.with_suffix(".md.bak"))
    try:
        importlib.reload(viveka_mod)
        results["fallback"] = assert_expect(
            "Fallback system prompt still contains 'Narada'",
            "Narada", viveka_mod.VIVEKA_SYSTEM_PROMPT, contains=True,
        )
    finally:
        system_path.with_suffix(".md.bak").rename(system_path)
        importlib.reload(viveka_mod)  # restore real version in module

    return results


# ── Test 2: Tasks.md parsing ────────────────────────────────────────


def test_tasks_parsing() -> dict:
    banner("TEST 2: tasks.md Active-section extraction")
    from prana.heartbeat.daemon import _extract_tasks_section

    content = (NARADA / "tasks.md").read_text(encoding="utf-8")
    active = _extract_tasks_section(content, "Active")
    done = _extract_tasks_section(content, "Done")
    missing = _extract_tasks_section(content, "ThisDoesNotExist")

    results = {}
    results["active_nonempty"] = assert_expect(
        "Active section non-empty",
        True, bool(active),
    )
    results["done_nonempty"] = assert_expect(
        "Done section non-empty",
        True, bool(done),
    )
    results["missing_empty"] = assert_expect(
        "Non-existent section returns empty string",
        "", missing,
    )
    results["active_format"] = assert_expect(
        "Active lines start with '- ['",
        "- [", active.splitlines()[0] if active else "", contains=True,
    )
    print(f"\n  Active section preview:")
    for line in active.splitlines()[:5]:
        print(f"    {line}")
    return results


# ── Test 3: Kill switch ─────────────────────────────────────────────


def test_kill_switch() -> dict:
    banner("TEST 3: Kill switch (~/.narada/heartbeat/pause)")
    pause_file = HEARTBEAT_DIR / "pause"
    results = {}

    # 3a: No pause file -&gt; beat_once should proceed normally (tested implicitly
    # by the pause_file.exists() check early in beat_once returning False)
    print("\n  3a: No pause file -&gt; not paused")
    results["no_pause"] = assert_expect(
        "pause file does not exist",
        False, pause_file.exists(),
    )

    # 3b: Create pause file and simulate the check in beat_once
    print("\n  3b: pause file exists -&gt; cycle would skip")
    pause_file.write_text("halt\n", encoding="utf-8")
    try:
        # Mirror the exact check in daemon.beat_once
        from prana.heartbeat.daemon import NARADA_ROOT
        check = (NARADA_ROOT / "heartbeat" / "pause").exists()
        results["pause_detected"] = assert_expect(
            "pause file detected",
            True, check,
        )
    finally:
        pause_file.unlink()
    results["cleanup"] = assert_expect(
        "pause file cleaned up",
        False, pause_file.exists(),
    )
    return results


# ── Test 4: Artifact detection ─────────────────────────────────────


def test_artifact_detection() -> dict:
    banner("TEST 4: New artifact detection (before/after set diff)")
    from prana.heartbeat.daemon import ingest_new_artifacts, ARTIFACTS_DIR

    results = {}
    # Snapshot before
    before = {p for p in ARTIFACTS_DIR.glob("*.md") if p.is_file()}
    print(f"  Before: {len(before)} existing artifacts")

    # Create a synthetic artifact (but don't invoke real ingest — use an empty
    # set as 'before' so our fake file is detected, then clean it up)
    fake = ARTIFACTS_DIR / "2026-04-15-dryrun-synthetic.md"
    fake.write_text(
        "---\ntopic: dryrun-synthetic\ndate: 2026-04-15\n---\n\n"
        "# Synthetic test artifact\n\nThis is a test artifact for dry-run purposes.\n",
        encoding="utf-8",
    )

    try:
        # With the original 'before', the new file should be in the diff
        current = {p for p in ARTIFACTS_DIR.glob("*.md") if p.is_file()}
        delta = sorted(current - before)
        print(f"  After: {len(current)} artifacts, delta: {len(delta)} new")

        results["delta_size"] = assert_expect(
            "Exactly one new artifact detected",
            1, len(delta),
        )
        results["delta_path"] = assert_expect(
            "New artifact path matches",
            str(fake), str(delta[0]),
        )
    finally:
        fake.unlink()  # clean up

    return results


# ── Test 5: Sandbox state capture ──────────────────────────────────


def test_sandbox_state() -> dict:
    banner("TEST 5: Sandbox git-state capture (pre/post execute check)")
    from prana.heartbeat.daemon import _project_git_head, check_sandbox_violations
    from prana.heartbeat.delegate import PROJECT_ROOT

    results = {}
    state = _project_git_head(PROJECT_ROOT)
    print(f"  Captured state (truncated): {state[:80] if state else 'None'}...")

    results["state_captured"] = assert_expect(
        "Git state captured (non-None)",
        True, state is not None,
    )

    # No violation when state matches itself
    print("\n  No change -&gt; no violation")
    violated = check_sandbox_violations(state, PROJECT_ROOT)
    results["no_violation"] = assert_expect(
        "Same state -&gt; no violation flag",
        False, violated,
    )

    # Simulated violation: compare state against a mutated version
    print("\n  Simulated state change -&gt; violation detected")
    violated = check_sandbox_violations("fake-different-hash|M something.py", PROJECT_ROOT)
    results["violation_detected"] = assert_expect(
        "Different state -&gt; violation flag raised",
        True, violated,
    )

    return results


# ── Test 6: Real smriti ingest on concrete artifact ────────────────


def test_real_ingest() -> dict:
    banner("TEST 6: Real smriti ingest on a concrete artifact (LIVE claude -p)")
    import subprocess

    results = {}

    # Create a realistic research-style artifact
    artifact = ARTIFACTS_DIR / "2026-04-15-dryrun-agentic-memory.md"
    artifact.write_text(
        """---
heartbeat_id: dryrun-001
date: 2026-04-15
topic: agentic memory systems
---

# Agentic memory systems — survey notes

I spent this cycle surveying how current AI systems handle persistent memory.
Three approaches stood out:

1. **Vector-store RAG** — embeds chunks, retrieves on query. Flexible but
   stateless: every query re-derives understanding from raw sources.
2. **Karpathy's LLM wiki pattern** — LLM maintains a structured markdown wiki,
   synthesizing at ingest time. Knowledge compounds. Requires human-defined
   schema.
3. **Self-organizing memory trees** — what smriti is building toward. Routing
   JUDGE finds where new content belongs, CREATE verdict lets the tree grow
   its own structure without a prescribed schema.

## Implications for smriti

The Karpathy pattern maps cleanly onto our cascade + routing design. The
difference is our CREATE verdict — the tree can self-organize.

Worth reviewing whether our routing prompt gives the JUDGE enough information
to make good CREATE vs LINK vs REVISE decisions.

## Related concepts
- [[projects/smriti]]
- retrieval-augmented generation
- knowledge graphs
""",
        encoding="utf-8",
    )
    print(f"  Created artifact: {artifact.relative_to(NARADA)}")
    print(f"  Size: {artifact.stat().st_size} bytes")

    # Invoke smriti ingest
    print("\n  Invoking: python -m smriti.cli ingest <path>")
    t0 = time.monotonic()
    proc = subprocess.run(
        ["python", "-m", "smriti.cli", "ingest", str(artifact)],
        capture_output=True, text=True, timeout=300,
    )
    elapsed = time.monotonic() - t0

    print(f"\n  Exit code: {proc.returncode}")
    print(f"  Elapsed: {elapsed:.1f}s")
    print(f"\n  --- STDOUT ---")
    for line in proc.stdout.splitlines():
        print(f"    {line}")
    if proc.stderr:
        print(f"\n  --- STDERR (tail) ---")
        for line in proc.stderr.splitlines()[-10:]:
            print(f"    {line}")

    results["exit_zero"] = assert_expect(
        "ingest exit code is 0",
        0, proc.returncode,
    )
    results["summary_created"] = assert_expect(
        "Summary page referenced in output",
        "Summary:", proc.stdout, contains=True,
    )
    results["routing_ran"] = assert_expect(
        "Routing phase ran",
        "Routing:", proc.stdout, contains=True,
    )

    return results


# ── Main ────────────────────────────────────────────────────────────


def main():
    print("HEARTBEAT MEMORY INTEGRATION — DRY RUN REPORT")
    print(f"Narada root: {NARADA}")
    print(f"Heartbeat dir: {HEARTBEAT_DIR}")
    print(f"Artifacts dir: {ARTIFACTS_DIR}")

    all_results: dict[str, dict] = {}
    all_results["prompts"] = test_prompt_loading()
    all_results["tasks"] = test_tasks_parsing()
    all_results["kill_switch"] = test_kill_switch()
    all_results["artifacts"] = test_artifact_detection()
    all_results["sandbox"] = test_sandbox_state()
    all_results["ingest"] = test_real_ingest()

    # Summary
    banner("SUMMARY")
    total = 0
    passed = 0
    for group, results in all_results.items():
        group_total = len(results)
        group_pass = sum(1 for v in results.values() if v)
        total += group_total
        passed += group_pass
        print(f"  {group:<15s} {group_pass}/{group_total}")
    print(f"\n  TOTAL: {passed}/{total}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
