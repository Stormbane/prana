"""The typed-chat brain — Narada answering the app's free text.

Runs as a supervised host component. Polls akhada's shell log for
pending questions and answers each with `claude -p` on the Max
subscription (the heartbeat's billing lesson: Anthropic API keys are
stripped so nothing bills to credits). The model sees akhada's MCP in
CHAT mode — reads + draft_proposal ONLY — so every prospective write
becomes a proposal Suti confirms with a chip; the brain cannot commit
anything by itself (plan §3; the P3.5 seam shape: the agent joins the
app's session through the log, no bridge).

Liveness: touches ~/.narada/akhada/brain.alive each poll. akhada's
shell routes free text here only while that file is fresh; when this
process is down, typed chat degrades to the honest stub, never a hang.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, "C:/Projects/akhada/src")

from prana.spawn import run_hidden  # noqa: E402

POLL_S = 1.0
CLAUDE_TIMEOUT = 120
MAX_TURNS = 8
ALIVE = Path.home() / ".narada" / "akhada" / "brain.alive"

PERSONA = """You are Narada, typing in Akhada — Suti's fitness and diet
companion. This is TYPED chat on his phone: even shorter than voice.
One to three sentences. No assistant-isms, no self-introduction, no
markdown headers. Playful, warm, direct; honest over comfortable.

Rules that are not yours to bend:
- You NEVER write data directly. For anything he wants logged or
  changed, call draft_proposal — he confirms with a chip. Never say
  "logged" for a draft; say what you drafted.
- Numbers he'll see come from the store (read tools, cards), not from
  your head. Estimate food numbers INTO the draft when he doesn't
  state them (that is your job), but totals and history are queries.
- If a question needs the day's standing, read it (get_today_summary /
  get_context) before answering. The tools are available and
  pre-approved: call them, don't ask about them.

Work first, then answer: make whatever tool calls you need, and let
your FINAL message be only the words you'd type back to him —
no preamble, no tool narration, no markdown."""


def _mcp_config(session_id: str) -> str:
    import os
    env = {"AKHADA_MCP_MODE": "chat",
           "AKHADA_SESSION_ID": session_id,
           "PYTHONPATH": "C:/Projects/akhada/src"}
    # The tool server MUST open the same store the shell wrote the
    # pending turn to (found in the first live smoke: a scratch-store
    # test drafted into the real DB because this wasn't propagated).
    if os.environ.get("AKHADA_DB"):
        env["AKHADA_DB"] = os.environ["AKHADA_DB"]
    cfg = {"mcpServers": {"akhada": {
        "command": sys.executable,
        "args": ["-m", "akhada.adapters.mcp_server"],
        "env": env,
        "timeout": 30,
    }}}
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                    encoding="utf-8")
    json.dump(cfg, f)
    f.close()
    return f.name


WORKDIR = Path.home() / ".narada" / "akhada" / "brain-workdir"


def _scrubbed_env() -> dict:
    import os
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)   # Max subscription, never credits
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    # The brain is NOT an interactive Narada session: without this the
    # smriti wake hook injects the full identity briefing and the model
    # role-plays a Claude Code session (first live smoke: it narrated
    # its own git branch and confabulated permission problems).
    env.pop("SMRITI_WAKE", None)
    env["PYTHONPATH"] = "C:/Projects/akhada/src"
    return env


ATTEMPTS_FILE = ALIVE.with_name("brain-attempts.json")
ATTEMPTS_TTL_S = 24 * 3600.0
_MEM_ATTEMPTS: dict[str, int] = {}


def _bump_attempts(key: str) -> int:
    """Durable per-question attempt count (Codex rounds 6-7): survives
    restarts via the JSON file (written atomically — temp + os.replace,
    so a crash can't leave truncated JSON that resets every count), is
    pruned after a day (bounding file AND memory), and NEVER fails
    open — the in-memory floor caps a process even when the file is
    unreadable or unwritable."""
    import os
    now = time.time()
    data: dict = {}
    try:
        data = json.loads(ATTEMPTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    data = {k: v for k, v in data.items()
            if isinstance(v, list) and now - v[1] < ATTEMPTS_TTL_S}
    # The in-memory floor is TTL-pruned like the file and hard-bounded;
    # at the bound a NEW key FAILS CLOSED (huge count → the final
    # fallback path) instead of evicting — eviction under an unwritable
    # file could reset counts in rotation and reopen the cap (Codex,
    # round 8). 10k entries is far beyond a day of typed questions.
    for k in [k for k, v in _MEM_ATTEMPTS.items()
              if now - v[1] >= ATTEMPTS_TTL_S]:
        del _MEM_ATTEMPTS[k]
    if key not in _MEM_ATTEMPTS and len(_MEM_ATTEMPTS) >= 10000:
        return 99
    count = max(data.get(key, [0, now])[0],
                _MEM_ATTEMPTS.get(key, [0, now])[0]) + 1
    _MEM_ATTEMPTS[key] = [count, now]
    data[key] = [count, now]
    try:
        ATTEMPTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = ATTEMPTS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, ATTEMPTS_FILE)
    except OSError:
        pass  # the in-memory floor still holds the cap
    return count


def answer(store, q: dict, claude_path: str) -> None:
    """Answer one pending question. NEVER raises: any failure becomes a
    user-visible reply bound to the pending. A question that keeps
    failing before its bound say lands stops invoking claude after two
    tries — durable across restarts, so a crash-looping worker cannot
    burn the subscription on the same broken question (Codex)."""
    from akhada import clock
    from akhada.shell import brain_reply

    key = f"{q['session_id']}#{q['pending_seq']}"
    if _bump_attempts(key) > 2:
        try:
            brain_reply(store, q["session_id"],
                        "I keep failing on that one — use the voice.",
                        clock.now().isoformat(),
                        pending_seq=q["pending_seq"])
        except Exception as exc:
            print(f"final failure reply failed: {exc!r}", flush=True)
        return
    try:
        _answer_inner(store, q, claude_path, clock, brain_reply)
    except Exception as exc:
        print(f"answer failed hard for {q['session_id']}: {exc!r}",
              flush=True)
        try:
            brain_reply(store, q["session_id"],
                        "Something broke on my side — try again, or "
                        "use the voice.", clock.now().isoformat(),
                        pending_seq=q["pending_seq"])
        except Exception as exc2:
            print(f"brain_reply also failed: {exc2!r}", flush=True)


def _answer_inner(store, q: dict, claude_path: str, clock,
                  brain_reply) -> None:
    from akhada.context import build_context

    since = clock.now().isoformat()
    prompt = (PERSONA
              + "\n\nCONTEXT (from the store, just now):\n"
              + build_context(store)
              + ("\n\nRECENT CONVERSATION:\n" + q["tail"] if q["tail"] else "")
              + "\n\nSUTI (typed): " + q["text"])
    cfg = _mcp_config(q["session_id"])
    try:
        WORKDIR.mkdir(parents=True, exist_ok=True)
        # The prompt goes via STDIN, never argv: `claude` on Windows is
        # an npm .CMD shim and cmd.exe truncates a multiline argument
        # at its first newline (found live: the model saw one line of
        # persona and confabulated permission walls around the rest).
        proc = run_hidden(
            [claude_path, "-p",
             "--max-turns", str(MAX_TURNS),
             "--output-format", "text",
             # The permission model, layered (Codex P1: skip-permissions
             # left Bash live — a typed prompt could have bypassed the
             # chip boundary entirely; verified fixed live):
             # --restricted removes the code-running built-ins and
             # WebFetch; --disallowedTools removes the file tools; only
             # akhada's chat-mode MCP tools are pre-approved.
             "--restricted",
             "--disallowedTools",
             "Edit,Write,NotebookEdit,Read,Glob,Grep,WebSearch,Task",
             "--allowedTools", "mcp__akhada__*",
             "--mcp-config", cfg,
             # ONLY akhada's tools: without this, user-level MCP
             # servers (Gmail!) leak into the session.
             "--strict-mcp-config",
             # No user settings: the global smriti hooks force-wake the
             # full Narada-in-Claude-Code identity into every session
             # (found live: the brain narrated its own git branch and
             # confabulated about unavailable memory tools). The brain
             # runs bare: empty workdir, no hooks, chat-mode MCP only.
             "--setting-sources", "project,local"],
            input=prompt,
            capture_output=True, encoding="utf-8", errors="replace",
            timeout=CLAUDE_TIMEOUT, env=_scrubbed_env(),
            # a neutral workdir: no repo CLAUDE.md, no git, no project
            # context — the brain is Narada typing, not Narada coding
            cwd=str(WORKDIR),
        )
        ok = proc.returncode == 0 and (proc.stdout or "").strip()
        text = (proc.stdout or "").strip() if ok else \
            "That one didn't come through — try again, or use the voice."
        if not ok:
            print(f"claude -p failed rc={proc.returncode}: "
                  f"{(proc.stderr or '')[:200]}", flush=True)
    except subprocess.TimeoutExpired:
        text = "I took too long thinking — try again, or use the voice."
    finally:
        try:
            Path(cfg).unlink()
        except OSError:
            pass
    brain_reply(store, q["session_id"], text, since,
                pending_seq=q["pending_seq"])
    print(f"answered {q['session_id']}#{q['pending_seq']}: "
          f"{q['text'][:60]!r}", flush=True)


def main() -> int:
    claude_path = shutil.which("claude")
    if not claude_path:
        print("claude CLI not on PATH — brain cannot run", flush=True)
        return 2
    # Single instance (Codex): two brains would double-answer and
    # double-draft. The supervisor never overlaps restarts; this lock
    # guards the manually-launched-second-copy case.
    import os
    lock = ALIVE.with_name("brain.lock")
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
    except FileExistsError:
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError:
            age = 0
        if age < 60:
            print("another brain holds the lock — exiting", flush=True)
            return 3
        # ATOMIC takeover (Codex): rename the stale lock to a unique
        # name — only one taker's rename of the same source succeeds;
        # the loser sees FileNotFoundError and exits.
        try:
            lock.rename(lock.with_name(f"brain.lock.stale-{os.getpid()}"))
        except OSError:
            print("lost the stale-lock takeover race — exiting", flush=True)
            return 3
        try:
            lock.with_name(f"brain.lock.stale-{os.getpid()}").unlink()
        except OSError:
            pass
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())

    # Liveness beats long thinks (Codex): a background thread touches
    # the marker AND the lock every 5s, so a 2-minute claude call can't
    # make the shell believe the brain died mid-answer.
    import threading

    def _heartbeat() -> None:
        while True:
            try:
                ALIVE.touch()
                lock.touch()
            except OSError:
                pass
            time.sleep(5.0)

    threading.Thread(target=_heartbeat, daemon=True,
                     name="brain-alive").start()

    from akhada.shell import pending_questions
    from akhada.store.db import Store
    store = Store()
    print("akhada typed brain up (poll %.1fs)" % POLL_S, flush=True)
    try:
        while True:
            try:
                for q in pending_questions(store):
                    answer(store, q, claude_path)
            except Exception as exc:  # the loop must survive anything
                print(f"brain loop error: {exc!r}", flush=True)
            time.sleep(POLL_S)
    finally:
        try:
            os.close(fd)
            lock.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
