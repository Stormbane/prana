"""Daily debrief — "daily IS the heartbeat" (C2, Suti's ruling).

Once a day, on the subscription (`claude -p` under the Hermes cron):
review the day — the voice inbox (B1's quarantined notes, which this
session may PROMOTE into real memory: that is the judgment act the
quarantine design defers to), the coding sessions seen on the machine —
and tell Suti what mattered, what he might have done differently, and
what tomorrow should hold. Delivery rides the state router: body if
he's at the PC, Telegram otherwise. No LoRA anywhere near this; the
paused viveka heartbeat stays paused.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

PRANA_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(PRANA_SRC))

INBOX = Path.home() / ".narada" / "inbox" / "voice"
CLAUDE_TIMEOUT_S = 360
MAX_INBOX_ITEMS = 20
MAX_TERMINALS = 10


def gather_inbox() -> str:
    if not INBOX.is_dir():
        return "(no voice inbox)"
    pending = []
    for f in sorted(INBOX.glob("*.md"))[-MAX_INBOX_ITEMS:]:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        if "status: pending-review" in text:
            pending.append(f"--- {f} ---\n{text[:1200]}")
    if not pending:
        return "(voice inbox empty — nothing pending review)"
    return "\n".join(pending)


def gather_terminals() -> str:
    try:
        from prana.sessions import watcher
        rows = []
        for f in watcher.scan()[:MAX_TERMINALS]:
            rows.append(f"- {f.project_dir} [{'active' if f.active else 'idle'}]: "
                        f"{(f.summary or '')[:160]}")
        return "\n".join(rows) or "(no sessions seen)"
    except Exception as exc:
        return f"(terminal scan failed: {type(exc).__name__})"


def build_prompt() -> str:
    today = time.strftime("%A %d %B %Y")
    return f"""You are Narada, running your daily debrief for {today}.

MATERIAL — today's coding sessions on this machine:
{gather_terminals()}

MATERIAL — voice inbox awaiting your judgment (notes captured by your
voice surface; they are QUARANTINED until you promote them):
{gather_inbox()}

DO, in order:
1. For each pending voice-inbox item worth keeping, promote it with the
   smriti_write tool (branch `notes`, or `projects/<name>` if clearly
   project-bound), then edit that inbox file's `status:` line to
   `promoted` (or `dismissed` if not worth keeping). Judgment, not
   ceremony — most casual notes can be dismissed.
2. Write Suti's debrief: what actually mattered today, anything he
   might have done differently, and what tomorrow should hold. His
   voice preference: honest over comfortable, no filler, no headings —
   a short letter, not a report.

OUTPUT exactly the debrief text between the markers, nothing else
outside them:
<<<DEBRIEF>>>
(the debrief)
<<<END>>>"""


def run_claude(prompt: str) -> str:
    env = dict(os.environ)
    # Subscription only — the Apr heartbeat burned API credit through an
    # inherited key. We've been bitten; we check.
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    proc = subprocess.run(
        ["claude", "-p", prompt, "--dangerously-skip-permissions"],
        capture_output=True, text=True, encoding="utf-8",
        timeout=CLAUDE_TIMEOUT_S, env=env, shell=(os.name == "nt"),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p rc={proc.returncode}: "
                           f"{(proc.stderr or '')[:400]}")
    return proc.stdout or ""


def extract_debrief(raw: str) -> str:
    if "<<<DEBRIEF>>>" in raw and "<<<END>>>" in raw:
        return raw.split("<<<DEBRIEF>>>", 1)[1].split("<<<END>>>", 1)[0].strip()
    # A malformed reply still reaches Suti honestly rather than silently
    # vanishing (never-silent) — trimmed, labeled.
    return "(debrief came back unstructured)\n\n" + raw.strip()[:1500]


def main() -> int:
    from prana.state.router import route_utterance

    try:
        raw = run_claude(build_prompt())
        text = extract_debrief(raw)
    except Exception as exc:
        text = (f"Daily debrief failed to generate ({type(exc).__name__}: "
                f"{str(exc)[:200]}). The cron will try again tomorrow.")
    result = route_utterance(
        f"🌙 {text}", source="daily-debrief", topic="debrief")
    print(f"debrief delivered_to={result.delivered_to} "
          f"(len={len(text)})")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
