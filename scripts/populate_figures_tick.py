"""Cron tick — kick off one Beautiful Tree figure-dossier generation.

Launched on schedule by the `Narada_Figures` Windows scheduled task
(retire-hermes migration, 2026-09-07 — this was a Hermes `--script` job
until then). This launches the real work DETACHED and returns
immediately: the generation takes ~20-40 min, far longer than a
scheduled tick should block, so we fire-and-forget and let it run
independently.

ENGINE — Codex since 2026-07-27 (Suti's ruling: the nightly Claude runs were
eating the Max weekly quota; Codex bills the ChatGPT subscription instead).
The Claude launch path is kept below behind ENGINE = "claude" for easy
rollback. Codex follows the repo playbook
`supabase/seed/figures-populate-codex.md` (the port of the figures-populate
skill; keep the two in sync).

HOME: this file lives in the prana repo (scripts/); the scheduled task
points at it directly. Its own log/lock/child-output live under
~/.narada/figures/ (narada-owned, decoupled from the retired Hermes
store) — see OUT_DIR.

Load-bearing details:

  1. Strip API keys from the child env. Generation MUST bill the subscription
     (ChatGPT for codex, Max for claude), never a raw API key. The BT repo
     carries keys in .env.local for its own tsx pipeline scripts, which
     dotenv-load them regardless of this env — only the CLI's billing is at
     stake here (cf. heartbeat Apr 12-15).
  2. Codex model comes from ~/.codex/config.toml (gpt-5.6-sol as of 07-27);
     no explicit pin so config stays the single lever. Claude pins Sonnet
     (the headless default, claude-fable-5, is not on Max 5x and errors out).
  3. Detached launch. The work outlives this tick; a job timeout can't kill it.
  4. Lock. If a generation started < LOCK_TTL ago, skip — never run two at
     once. Codex runtime is less proven than Claude's ~18 min, so the TTL is
     55 min (just under the hourly cadence).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ENGINE = "codex"  # "codex" | "claude"

CODEX = Path("C:/Users/admin/AppData/Local/Programs/OpenAI/Codex/bin/codex.exe")
CLAUDE = Path("C:/Users/admin/.local/bin/claude.exe")
BT_ROOT = Path("C:/Projects/beautiful-tree")
CLAUDE_MODEL = "claude-sonnet-4-6"
# narada-owned (was ~/.hermes/cron/output under Hermes); holds this
# tick's own launch log, the run-lock, and the detached child's stdout.
OUT_DIR = Path.home() / ".narada" / "figures"
LOG = OUT_DIR / "figures-populate.log"
LOCK = OUT_DIR / "figures-populate.lock"
LOCK_TTL = 55 * 60  # codex runtime unproven; stay under the hourly cadence

CODEX_PROMPT = (
    "Read supabase/seed/figures-populate-codex.md and execute it for 1 figure "
    "(N=1). Follow it exactly, including the report format."
)

# Windows flags: the child outlives this process AND stays invisible.
# CREATE_NO_WINDOW (not DETACHED_PROCESS): DETACHED gives the child NO console,
# so every console-app grandchild (powershell/cmd/node that codex spawns per
# command) allocated its own VISIBLE window and stole focus (Suti, 07-27).
# CREATE_NO_WINDOW gives the child a hidden console that grandchildren inherit.
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if LOCK.exists() and (time.time() - LOCK.stat().st_mtime) < LOCK_TTL:
        age = int(time.time() - LOCK.stat().st_mtime)
        print(f"[populate-figures] a run started {age}s ago is likely still active; skipping")
        return 0

    binary = CODEX if ENGINE == "codex" else CLAUDE
    if not binary.exists():
        print(f"[populate-figures] {ENGINE} not found at {binary}", file=sys.stderr)
        return 1

    env = dict(os.environ)
    # (1) Force subscription billing, never a raw API key.
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    env.pop("OPENAI_API_KEY", None)
    env.pop("OPENAI_BASE_URL", None)

    if ENGINE == "codex":
        cmd = [
            str(CODEX), "exec",
            "--dangerously-bypass-approvals-and-sandbox",  # unattended: runs npx tsx + file writes
            "--skip-git-repo-check",
            "-C", str(BT_ROOT),
            "-c", "tools.web_search=true",  # references stage verifies URLs
            CODEX_PROMPT,
        ]
        label = f"codex exec figures playbook ({ENGINE})"
    else:
        # Headless claude -p kills background tasks after 600s by default,
        # which murdered every ~18-min generation (Mozart, Jul 2).
        env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] = "0"
        cmd = [
            str(CLAUDE),
            "-p", "/figures-populate 1",
            "--model", CLAUDE_MODEL,
            "--dangerously-skip-permissions",
            "--max-turns", "30",
        ]
        label = f"claude -p /figures-populate 1 ({CLAUDE_MODEL})"

    # (4) Claim the lock before launching.
    LOCK.write_text(str(time.time()), encoding="utf-8")

    with open(LOG, "a", encoding="utf-8") as logf:
        logf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} launch {label} =====\n")
        logf.flush()
        # (3) Detached: returns immediately, child runs on its own.
        subprocess.Popen(
            cmd,
            cwd=str(BT_ROOT),
            env=env,
            stdout=logf,
            stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )

    print(f"[populate-figures] launched detached: {label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
