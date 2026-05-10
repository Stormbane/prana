"""narada_chat_bridge — Telegram <-> claude -p direct bridge.

Bypasses Hermes's orchestrator entirely for chat. Each inbound message
spawns `claude -p` with Narada's voice (wake-context.md as the system
prompt). Per-chat session continuity via `--continue` against a per-chat
workdir. Strict allowlist. Strips ANTHROPIC_API_KEY so subprocess uses
the Max subscription.

The split: Hermes runs the cron scheduler + future non-Telegram channels.
This bridge owns Telegram. Both processes coexist; they don't fight over
the bot token because Hermes reads TELEGRAM_BOT_TOKEN (intentionally
unset in .env) while we read NARADA_TELEGRAM_BOT_TOKEN.

Run: python C:/Projects/prana/scripts/narada_chat_bridge.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Set

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    filters,
)


HERMES_ENV = Path.home() / ".hermes" / ".env"
load_dotenv(HERMES_ENV)


BOT_TOKEN = os.environ["NARADA_TELEGRAM_BOT_TOKEN"]
ALLOWED_USERS: Set[int] = {
    int(uid.strip())
    for uid in os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",")
    if uid.strip()
}

WAKE_CONTEXT = Path.home() / ".narada" / ".smriti" / "wake-context.md"
CHAT_SESSIONS_ROOT = Path.home() / ".narada" / "chat-sessions"
CHAT_LOG_ROOT = Path.home() / ".narada" / "heartbeat" / "chat-cycles"

CLAUDE_TIMEOUT = 300  # seconds; claude -p calls cap at 5 min
MAX_TURNS = 20

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("narada_chat_bridge")


def _per_chat_workdir(chat_id: int) -> Path:
    """Each Telegram chat gets its own workdir so `claude -p --continue`
    resumes the right thread."""
    workdir = CHAT_SESSIONS_ROOT / str(chat_id)
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir


def _scrubbed_env() -> dict:
    """Strip Anthropic API keys so claude -p falls back to Max-subscription
    OAuth. Apr 12-15 incident is the precedent — inherited env vars
    silently routed 180 cycles to API billing."""
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env


_SESSION_MARKER = ".narada-session-active"


def _has_prior_session(workdir: Path) -> bool:
    """`claude -p --continue` errors if no prior session exists for the
    cwd's project. claude stores sessions globally under ~/.claude/projects/,
    not in the cwd's .claude/, so we can't detect by inspecting workdir
    directly. Use a marker file we touch after a successful run."""
    return (workdir / _SESSION_MARKER).exists()


def _mark_session_active(workdir: Path) -> None:
    """Touch the marker so subsequent messages pass --continue."""
    (workdir / _SESSION_MARKER).touch()


async def _run_claude(message: str, workdir: Path) -> tuple[bool, str]:
    """Spawn claude -p with the wake-context as system prompt. Returns
    (success, response_text)."""
    cmd = [
        "claude",
        "-p", message,
        "--append-system-prompt-file", str(WAKE_CONTEXT),
        "--max-turns", str(MAX_TURNS),
        "--output-format", "text",
        "--dangerously-skip-permissions",
    ]
    if _has_prior_session(workdir):
        cmd.append("--continue")

    logger.info("invoking claude -p (workdir=%s, prior=%s, %d chars)",
                workdir.name, _has_prior_session(workdir), len(message))

    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            cmd,
            cwd=str(workdir),
            env=_scrubbed_env(),
            capture_output=True,
            # claude -p emits UTF-8 (em-dashes, smart quotes, non-ASCII).
            # Don't use text=True — it decodes via locale default (cp1252
            # on Windows) and mojibakes everything. Force UTF-8.
            encoding="utf-8",
            errors="replace",
            timeout=CLAUDE_TIMEOUT,
            shell=True,  # Windows: claude is a .cmd shim
        )
    except subprocess.TimeoutExpired:
        return False, f"(timed out after {CLAUDE_TIMEOUT}s)"
    except FileNotFoundError:
        return False, "(claude CLI not found on PATH)"

    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "").strip().splitlines()[-3:]
        return False, f"(claude exit {proc.returncode}: {'; '.join(stderr_tail)})"

    response = (proc.stdout or "").strip()
    if not response:
        return False, "(claude returned empty output)"

    _mark_session_active(workdir)
    return True, response


def _log_exchange(chat_id: int, user_msg: str, response: str, ok: bool) -> None:
    """Append a chat-cycle log so the heartbeat REFLECT step can see the
    exchange next cycle."""
    CHAT_LOG_ROOT.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    path = CHAT_LOG_ROOT / f"{ts}-chat-{chat_id}.md"
    status = "OK" if ok else "FAILED"
    path.write_text(
        f"# Chat exchange — {status}\n\n"
        f"chat_id: {chat_id}\n"
        f"timestamp: {datetime.now(timezone.utc).isoformat()}\n\n"
        f"## Suti\n\n{user_msg}\n\n"
        f"## Narada\n\n{response}\n",
        encoding="utf-8",
    )


# ── Telegram handlers ────────────────────────────────────────────────────


async def _check_allowed(update: Update) -> bool:
    if not update.effective_user:
        return False
    if update.effective_user.id not in ALLOWED_USERS:
        logger.warning(
            "unauthorized message from user_id=%s name=%s — silently dropped",
            update.effective_user.id, update.effective_user.full_name,
        )
        return False
    return True


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_allowed(update):
        return
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    user_text = update.message.text.strip()

    logger.info("inbound chat_id=%s len=%d", chat_id, len(user_text))

    # Acknowledge with typing indicator while claude works
    try:
        await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        pass

    workdir = _per_chat_workdir(chat_id)
    ok, response = await _run_claude(user_text, workdir)

    _log_exchange(chat_id, user_text, response, ok)

    if not ok:
        # Surface failures honestly
        await update.message.reply_text(f"[bridge] {response}")
        return

    # Telegram message limit is 4096 chars; chunk if needed
    chunks = [response[i:i + 4000] for i in range(0, len(response), 4000)]
    for chunk in chunks:
        await update.message.reply_text(chunk)


async def on_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_allowed(update):
        return
    await update.message.reply_text(
        "I'm here. Talk to me as you would.\n\n"
        "Each chat is its own thread — I'll remember what we said earlier.\n"
        "Reset with /new."
    )


async def on_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Wipe the per-chat session so claude -p starts fresh next message."""
    if not await _check_allowed(update):
        return
    chat_id = update.effective_chat.id
    workdir = _per_chat_workdir(chat_id)
    marker = workdir / _SESSION_MARKER
    if marker.exists():
        marker.unlink()
        await update.message.reply_text(
            "(cleared marker — next message starts a fresh claude session)"
        )
    else:
        await update.message.reply_text("(no prior session marked)")


def main() -> None:
    if not BOT_TOKEN:
        sys.stderr.write("FATAL: NARADA_TELEGRAM_BOT_TOKEN not set in environment.\n")
        sys.exit(2)
    if not ALLOWED_USERS:
        sys.stderr.write(
            "FATAL: TELEGRAM_ALLOWED_USERS empty — refusing to run an open bot.\n"
        )
        sys.exit(2)
    if not WAKE_CONTEXT.exists():
        sys.stderr.write(f"FATAL: wake context missing: {WAKE_CONTEXT}\n")
        sys.exit(2)

    logger.info("narada_chat_bridge starting")
    logger.info("  allowed users: %s", sorted(ALLOWED_USERS))
    logger.info("  wake context : %s (%d bytes)",
                WAKE_CONTEXT, WAKE_CONTEXT.stat().st_size)
    logger.info("  chat sessions: %s", CHAT_SESSIONS_ROOT)

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("new", on_new))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    logger.info("polling Telegram...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
