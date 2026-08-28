"""Host alerting — a sick component pages Suti's Telegram (A3).

Never silent: the zombie-heartbeat lesson (and the week LiveKit sat
dead behind a green probe) is that failure without a page is the worst
failure mode. This module turns supervisor lifecycle *transitions* into
at-most-one Telegram alert per failure episode, with durable state in
~/.narada/state.db so neither respawns nor host restarts can re-page or
lose an undelivered alert.

Design (cross-review round 1 #3 + round 2 R2-1):
- The supervisor calls the record_* hooks on explicit transitions
  (spawn, exit, health probe result, health-fail termination, cooldown).
  Probe successes persist a healthy-since timestamp; time-based rules
  cannot arrive as events, so a durable SWEEP (startup + every 60 s)
  evaluates open episodes against persisted timestamps.
- Alert transitions per component:
    (a) >= 3 health-fail terminations within a rolling 30 min;
    (b) cooldown entered (the supervisor's real "giving up for now");
    (c) episode open >= 30 min and the component is not healthy.
- One open episode per component; an episode alerts once. Recovery
  (healthy >= 5 min) closes it with a single recovery message. An
  episode that never alerted closes silently — transient blips under
  the thresholds are not pages.
- Delivery is a durable OUTBOX: enqueue, then send with backoff and
  429 Retry-After handling; delivered only on 2xx. Undelivered alerts
  survive host restarts and send when connectivity returns.
- Diagnostics are bounded and redacted — the bridge logs its bot-token
  URL, which must never reach a phone notification.

Telegram credentials come from ~/.hermes/.env (the bridge's config,
read directly — the bridge *process* may be the sick component, so the
send path depends on nothing but stdlib HTTP).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from prana.state.db import get_db
from prana.voice.transcripts import redact as _voice_redact

logger = logging.getLogger("prana.host.alerts")

HERMES_ENV = Path.home() / ".hermes" / ".env"

SWEEP_INTERVAL_S = 60.0
EPISODE_ALERT_DEADLINE_S = 30 * 60.0   # (c) unhealthy this long -> page
HFT_STORM_N = 3                        # (a) health-fail terminations...
HFT_STORM_WINDOW_S = 30 * 60.0         #     ...within this window
RECOVERY_HEALTHY_S = 5 * 60.0          # healthy this long -> episode closes
DIAG_MAX_CHARS = 300
SEND_BACKOFF_BASE_S = 60.0
SEND_BACKOFF_MAX_S = 3600.0

# Telegram bot tokens ("1234567890:AAG...") appear in bridge log lines
# (often as ".../bot1234567890:AAG.../getUpdates", where "bot" glues to
# the digits and defeats a leading \b); the voice-layer redactor doesn't
# know this shape. No leading boundary on purpose.
_TG_TOKEN = re.compile(r"\d{8,12}:[A-Za-z0-9_-]{30,}\b")


def _redact(text: str) -> str:
    return _TG_TOKEN.sub("[REDACTED]", _voice_redact(text))


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS host_alert_status (
    component       TEXT PRIMARY KEY,
    healthy_since   REAL,               -- epoch; NULL = not currently healthy
    episode_open    INTEGER NOT NULL DEFAULT 0,
    episode_opened_at REAL,
    episode_alerted_at REAL,
    last_event_at   REAL,
    last_diag       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS host_alert_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    component   TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- spawned|exit|health-fail|health-ok|hft|cooldown|alerted|recovered
    detail      TEXT NOT NULL DEFAULT '',
    at          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alert_events_component
    ON host_alert_events(component, kind, at);

CREATE TABLE IF NOT EXISTS host_alert_outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      REAL NOT NULL,
    text            TEXT NOT NULL,
    delivered_at    REAL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error      TEXT
);
CREATE INDEX IF NOT EXISTS idx_alert_outbox_pending
    ON host_alert_outbox(next_attempt_at) WHERE delivered_at IS NULL;
"""


def _load_telegram_config() -> Optional[tuple[str, str]]:
    """(bot_token, chat_id) from the bridge's env file, or None."""
    try:
        from dotenv import dotenv_values
        vals = dotenv_values(HERMES_ENV)
    except Exception:
        return None
    token = (vals or {}).get("NARADA_TELEGRAM_BOT_TOKEN")
    users = (vals or {}).get("TELEGRAM_ALLOWED_USERS", "")
    chat = users.split(",")[0].strip() if users else ""
    if not token or not chat:
        return None
    return token, chat


def _telegram_send(token: str, chat_id: str, text: str) -> tuple[bool, Optional[float], str]:
    """POST sendMessage. Returns (delivered, retry_after_s, error)."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            if 200 <= resp.status < 300:
                return True, None, ""
            return False, None, f"status {resp.status}"
    except urllib.error.HTTPError as exc:
        retry_after: Optional[float] = None
        if exc.code == 429:
            try:
                payload = json.loads(exc.read().decode("utf-8", "replace"))
                retry_after = float(
                    payload.get("parameters", {}).get("retry_after", 60))
            except Exception:
                retry_after = 60.0
        return False, retry_after, f"HTTP {exc.code}"
    except Exception as exc:
        return False, None, f"{type(exc).__name__}"


class AlertManager:
    """Durable episode state machine + outbox. One instance per host."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        send: Optional[Callable[[str], tuple[bool, Optional[float], str]]] = None,
        now: Callable[[], float] = time.time,
    ):
        self._db_path = db_path
        self._now = now
        self._send = send  # tests inject; None = real Telegram, lazy config
        self._conn = get_db(db_path) if db_path else get_db()
        self._conn.executescript(SCHEMA_SQL)
        self._config_warned = False
        # Last stderr line per component, in memory: stderr is chatty
        # (the bridge logs every poll), so it is persisted into
        # last_diag only at failure transitions — one write per episode
        # event, not one per log line.
        self._last_diag: dict[str, str] = {}

    # ── recording hooks (called by the supervisor; must stay cheap) ──

    def _status_row(self, component: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO host_alert_status (component) VALUES (?)",
            (component,))

    def _event(self, component: str, kind: str, detail: str = "") -> None:
        now = self._now()
        self._status_row(component)
        self._conn.execute(
            "INSERT INTO host_alert_events (component, kind, detail, at) "
            "VALUES (?, ?, ?, ?)", (component, kind, detail[:DIAG_MAX_CHARS], now))
        self._conn.execute(
            "UPDATE host_alert_status SET last_event_at = ? WHERE component = ?",
            (now, component))

    def _mark_unhealthy(self, component: str) -> None:
        """Any failure transition: clear healthy-since, open an episode,
        persist the freshest diagnostic line."""
        now = self._now()
        self._status_row(component)
        self._conn.execute(
            "UPDATE host_alert_status SET healthy_since = NULL, "
            "episode_open = 1, "
            "episode_opened_at = COALESCE(episode_opened_at, ?), "
            "last_diag = ? "
            "WHERE component = ?",
            (now, self._last_diag.get(component, ""), component))

    def record_spawned(self, component: str, has_health_probe: bool) -> None:
        # A component with no probe has only liveness to go on: a spawn
        # is tentatively healthy (an early exit will clear it). A probed
        # component is healthy only when its probe says so — including
        # after a host restart: a stale persisted healthy_since could
        # otherwise age past the recovery threshold during downtime and
        # close an episode before the new process passed a single probe
        # (Codex review P2). Clear it; the first health-ok restarts the
        # recovery clock.
        self._event(component, "spawned")
        if has_health_probe:
            self._conn.execute(
                "UPDATE host_alert_status SET healthy_since = NULL "
                "WHERE component = ?", (component,))
        else:
            self._conn.execute(
                "UPDATE host_alert_status SET healthy_since = "
                "COALESCE(healthy_since, ?) WHERE component = ?",
                (self._now(), component))

    def record_exit(self, component: str, rc: int) -> None:
        self._event(component, "exit", f"rc={rc}")
        self._mark_unhealthy(component)

    def record_health(self, component: str, ok: bool, detail: str = "") -> None:
        self._status_row(component)
        if ok:
            row = self._conn.execute(
                "SELECT healthy_since FROM host_alert_status WHERE component = ?",
                (component,)).fetchone()
            if row is None or row["healthy_since"] is None:
                self._event(component, "health-ok")
                self._conn.execute(
                    "UPDATE host_alert_status SET healthy_since = ? "
                    "WHERE component = ?", (self._now(), component))
        else:
            self._event(component, "health-fail", detail)
            self._mark_unhealthy(component)

    def record_health_fail_termination(self, component: str) -> None:
        self._event(component, "hft")
        self._mark_unhealthy(component)

    def record_cooldown(self, component: str) -> None:
        self._event(component, "cooldown")
        self._mark_unhealthy(component)

    def note_diagnostic(self, component: str, line: str) -> None:
        """Bounded, redacted last-stderr-line — in memory only (stderr
        is high-volume; persisted at failure transitions)."""
        line = _redact(line.strip())[:DIAG_MAX_CHARS]
        if line:
            self._last_diag[component] = line

    # ── the sweep (startup + every 60 s) ──

    def sweep(self) -> None:
        """Evaluate every open episode against durable timestamps."""
        now = self._now()
        rows = self._conn.execute(
            "SELECT * FROM host_alert_status WHERE episode_open = 1").fetchall()
        for row in rows:
            name = row["component"]
            healthy_since = row["healthy_since"]
            alerted = row["episode_alerted_at"] is not None

            # Recovery / silent close: healthy long enough.
            if healthy_since is not None and (now - healthy_since) >= RECOVERY_HEALTHY_S:
                self._conn.execute(
                    "UPDATE host_alert_status SET episode_open = 0, "
                    "episode_opened_at = NULL, episode_alerted_at = NULL "
                    "WHERE component = ?", (name,))
                self._event(name, "recovered")
                if alerted:
                    self._enqueue(
                        f"✅ {name} recovered — healthy for "
                        f"{int(RECOVERY_HEALTHY_S / 60)} min.")
                continue

            if alerted:
                continue  # one alert per episode

            reason = self._alert_reason(name, row, now)
            if reason:
                self._conn.execute(
                    "UPDATE host_alert_status SET episode_alerted_at = ? "
                    "WHERE component = ?", (now, name))
                self._event(name, "alerted", reason)
                mins = int((now - (row["episode_opened_at"] or now)) / 60)
                diag = row["last_diag"] or "(no diagnostic captured)"
                self._enqueue(
                    f"🔴 {name} is sick: {reason} (episode {mins} min). "
                    f"Last stderr: {diag}")

    def _alert_reason(self, name: str, row, now: float) -> Optional[str]:
        cutoff = now - HFT_STORM_WINDOW_S
        opened = row["episode_opened_at"] or now
        n_hft = self._conn.execute(
            "SELECT COUNT(*) AS n FROM host_alert_events "
            "WHERE component = ? AND kind = 'hft' AND at >= ?",
            (name, cutoff)).fetchone()["n"]
        if n_hft >= HFT_STORM_N:
            return f"{n_hft} health-fail terminations in 30 min"
        n_cd = self._conn.execute(
            "SELECT COUNT(*) AS n FROM host_alert_events "
            "WHERE component = ? AND kind = 'cooldown' AND at >= ?",
            (name, opened)).fetchone()["n"]
        if n_cd > 0:
            return "restart storm — supervisor entered cooldown"
        if (now - opened) >= EPISODE_ALERT_DEADLINE_S and row["healthy_since"] is None:
            return f"unhealthy for {int((now - opened) / 60)} min"
        return None

    # ── the outbox ──

    def _enqueue(self, text: str) -> None:
        self._conn.execute(
            "INSERT INTO host_alert_outbox (created_at, text, next_attempt_at) "
            "VALUES (?, ?, 0)", (self._now(), _redact(text)))

    def drain_outbox(self) -> None:
        now = self._now()
        rows = self._conn.execute(
            "SELECT * FROM host_alert_outbox WHERE delivered_at IS NULL "
            "AND next_attempt_at <= ? ORDER BY id ASC LIMIT 10", (now,)).fetchall()
        if not rows:
            return
        send = self._send
        if send is None:
            cfg = _load_telegram_config()
            if cfg is None:
                if not self._config_warned:
                    logger.warning(
                        "no Telegram config in %s — alerts queue durably "
                        "but cannot deliver", HERMES_ENV)
                    self._config_warned = True
                return
            token, chat = cfg
            send = lambda text: _telegram_send(token, chat, text)  # noqa: E731

        for row in rows:
            delivered, retry_after, err = send(row["text"])
            if delivered:
                self._conn.execute(
                    "UPDATE host_alert_outbox SET delivered_at = ?, "
                    "attempts = attempts + 1 WHERE id = ?",
                    (self._now(), row["id"]))
                logger.info("alert delivered: %s", row["text"][:80])
            else:
                attempts = row["attempts"] + 1
                backoff = retry_after if retry_after is not None else min(
                    SEND_BACKOFF_BASE_S * (2 ** min(attempts - 1, 6)),
                    SEND_BACKOFF_MAX_S)
                self._conn.execute(
                    "UPDATE host_alert_outbox SET attempts = ?, "
                    "next_attempt_at = ?, last_error = ? WHERE id = ?",
                    (attempts, self._now() + backoff, err, row["id"]))
                logger.warning(
                    "alert send failed (%s), retry in %.0fs", err, backoff)

    # ── the loop ──

    async def run(self, shutdown: asyncio.Event) -> None:
        """Startup sweep, then every SWEEP_INTERVAL_S until shutdown."""
        while True:
            try:
                await asyncio.to_thread(self.sweep)
                await asyncio.to_thread(self.drain_outbox)
            except Exception:
                logger.exception("alert sweep failed — will retry")
            try:
                await asyncio.wait_for(
                    shutdown.wait(), timeout=SWEEP_INTERVAL_S)
                return
            except asyncio.TimeoutError:
                continue
