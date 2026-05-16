"""Heartbeat daemon — the autonomous identity loop.

Desire (local) → Intention (frontier) → Judgment (local) → Execute → Remember

This is the heartbeat. It runs continuously. It wakes, desires, delegates,
judges, and remembers. It is the first step toward continuity.

Usage:
    python -m prana.heartbeat
    python -m prana.heartbeat --interval 1800  # 30 minutes
    python -m prana.heartbeat --once            # single heartbeat

Everything runtime — prompts, state sources, cycle-log paths — is declared
in ``~/.narada/heartbeat/wake.md``. See :mod:`prana.heartbeat.wake` and
``examples/narada-install/`` for the manifest format.
"""

from __future__ import annotations

import logging
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from deha.expression.expression import ExpressionClient
from deha.display import DisplayClient
from prana.indriyas.jnanendriyas.tvac.weather import fetch_kallangur_weather
from prana.heartbeat.cycle_log import CycleRecord, latest_started, write_cycle
from prana.heartbeat.delegate import ClaudeDelegate
from prana.heartbeat.viveka import Action, Desire, VivekaCore
from prana.heartbeat.wake import (
    NARADA_ROOT,
    WakeManifest,
    WakeManifestError,
    cycle_log_path,
    load_manifest,
    resolve_state,
)

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL = 1800  # 30 minutes
MAX_REVISIONS = 2

ARTIFACTS_DIR = NARADA_ROOT / "heartbeat" / "artifacts"
MESSAGES_DIR = NARADA_ROOT / "heartbeat" / "messages"
PAUSE_FILE = NARADA_ROOT / "heartbeat" / "pause"
LAST_SLEEP_MARKER = NARADA_ROOT / ".smriti" / "last-sleep"
SMTP_CONFIG_FILE = NARADA_ROOT / "heartbeat" / ".smtp-config"
PROJECT_GIT_ROOT = Path(__file__).resolve().parents[3]


# ── Email transport for CHECK_IN ────────────────────────────────────


def _load_smtp_config() -> dict[str, str] | None:
    """Load SMTP config from ~/.narada/heartbeat/.smtp-config.

    Format: KEY=VALUE, one per line. Comments with #. Returns None if
    the file doesn't exist — email sending is then silently skipped.
    """
    if not SMTP_CONFIG_FILE.exists():
        return None
    cfg: dict[str, str] = {}
    try:
        for line in SMTP_CONFIG_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            cfg[key.strip()] = value.strip()
    except OSError as exc:
        logger.warning("Could not read SMTP config: %s", exc)
        return None
    required = ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_TO")
    if not all(k in cfg for k in required):
        logger.warning(
            "SMTP config at %s missing required keys (needs %s)",
            SMTP_CONFIG_FILE, ", ".join(required),
        )
        return None
    return cfg


def _send_check_in_email(subject: str, body: str) -> bool:
    """Send the CHECK_IN message as an email.

    Returns True on success, False on any failure (which is non-fatal
    for the cycle — the message file is still written). Reads config
    from `~/.narada/heartbeat/.smtp-config`; if the file is missing or
    incomplete, returns False without raising.
    """
    cfg = _load_smtp_config()
    if cfg is None:
        return False

    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = subject
    mail_from = cfg.get("MAIL_FROM") or cfg["SMTP_USER"]
    from_name = cfg.get("MAIL_FROM_NAME", "").strip()
    msg["From"] = f"{from_name} <{mail_from}>" if from_name else mail_from
    msg["To"] = cfg["MAIL_TO"]
    msg.set_content(body)

    host = cfg["SMTP_HOST"]
    port = int(cfg.get("SMTP_PORT", "587"))

    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(cfg["SMTP_USER"], cfg["SMTP_PASS"])
            smtp.send_message(msg)
        return True
    except (smtplib.SMTPException, OSError) as exc:
        logger.warning("CHECK_IN email send failed: %s", exc)
        return False


# ── Artifact → smriti ingest ────────────────────────────────────────


def ingest_new_artifacts(before: set[Path]) -> list[dict]:
    """Ingest artifacts created during this cycle into the smriti memory tree.

    Identifies files in ``~/.narada/heartbeat/artifacts/`` that weren't
    present before the cycle started, runs ``smriti ingest`` on each.
    Failures are logged but don't abort the heartbeat.
    """
    if not ARTIFACTS_DIR.exists():
        return []

    from prana.heartbeat.delegate import _log_rate_limit, _looks_like_rate_limit

    current = {p for p in ARTIFACTS_DIR.glob("*.md") if p.is_file()}
    new_artifacts = sorted(current - before)

    results = []
    for artifact in new_artifacts:
        logger.info("Ingesting artifact: %s", artifact.name)
        try:
            proc = subprocess.run(
                ["python", "-m", "smriti.cli", "ingest", str(artifact)],
                capture_output=True,
                text=True,
                timeout=300,
            )
            results.append({
                "artifact": artifact.name,
                "ok": proc.returncode == 0,
                "stdout_tail": proc.stdout[-500:] if proc.stdout else "",
                "stderr_tail": proc.stderr[-500:] if proc.stderr else "",
            })
            if proc.returncode != 0:
                combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
                if _looks_like_rate_limit(combined):
                    _log_rate_limit(source="smriti-ingest", detail=combined[:500])
                logger.warning("Ingest failed for %s: %s", artifact.name, proc.stderr[:200])
            else:
                logger.info("Ingest succeeded for %s", artifact.name)
        except subprocess.TimeoutExpired:
            logger.warning("Ingest timed out for %s", artifact.name)
            results.append({"artifact": artifact.name, "ok": False, "error": "timeout"})
        except Exception as exc:
            logger.warning("Ingest crashed for %s: %s", artifact.name, exc)
            results.append({"artifact": artifact.name, "ok": False, "error": str(exc)})

    return results


# ── Sandbox violation detection ────────────────────────────────────


def _project_git_head(project_root: Path) -> str | None:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(project_root), capture_output=True, text=True, timeout=5,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(project_root), capture_output=True, text=True, timeout=5,
        )
        if head.returncode == 0 and status.returncode == 0:
            return f"{head.stdout.strip()}|{status.stdout.strip()}"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def check_sandbox_violations(
    project_state_before: str | None, project_root: Path
) -> bool:
    after = _project_git_head(project_root)
    if project_state_before is None or after is None:
        return False
    if project_state_before != after:
        logger.warning(
            "SANDBOX VIOLATION: project files changed during heartbeat execute. "
            "Before: %s ... After: %s ...",
            project_state_before[:80], after[:80],
        )
        return True
    return False


# ── Daemon ──────────────────────────────────────────────────────────


class HeartbeatDaemon:
    """The autonomous heartbeat loop."""

    def __init__(
        self,
        viveka: VivekaCore,
        delegate: ClaudeDelegate,
        *,
        manifest: WakeManifest,
        display: DisplayClient | None = None,
        expression: ExpressionClient | None = None,
        interval: int = DEFAULT_INTERVAL,
        weather_interval_s: int = 3600,
    ):
        self.viveka = viveka
        self.delegate = delegate
        self.manifest = manifest
        self.display = display or DisplayClient()
        self.expression = expression or ExpressionClient()
        self.interval = interval
        self.weather_interval_s = weather_interval_s
        # 0.0 = never pulled — first beat after startup always pulls.
        self._last_weather_pull: float = 0.0
        self._running = False

    def _maybe_pull_weather(self) -> None:
        """Pull current Kallangur weather and push to body if interval elapsed.

        Fail-soft: any error logged at WARNING and the cycle continues. Weather
        is non-essential to cognition — missing a pull just means the visual
        lags reality for a bit.

        Future: this hourly tick is one example of the broader "managed
        schedule" the heartbeat needs (Suti, 2026-04-25). When that lands,
        this will become a registered scheduled task rather than a
        special-cased call here.
        """
        now = time.time()
        if now - self._last_weather_pull < self.weather_interval_s:
            return
        try:
            weather = fetch_kallangur_weather()
        except Exception as exc:
            logger.warning("Weather fetch raised: %s: %s", type(exc).__name__, exc)
            return
        if weather is None:
            return  # already logged by fetcher
        if self.expression.set_weather(weather):
            self._last_weather_pull = now
            logger.info(
                "Weather pushed to body: %.1f°C, %.1f km/h @ %.0f°, "
                "precip %.2f mm/h, cloud %.0f%%",
                weather.temperature_c,
                weather.wind_speed_kmh,
                weather.wind_direction_deg,
                weather.precipitation_mm_hr,
                weather.cloud_cover_pct,
            )
        else:
            logger.warning("Weather push to body failed; will retry next interval.")

    def beat_once(self) -> dict:
        """Execute a single heartbeat cycle.

        Returns:
            Dict with cycle results for logging/display.
        """
        now = datetime.now(timezone.utc)
        logger.info("=== HEARTBEAT %s ===", now.isoformat())

        # Scheduled side-task: refresh body's weather state once per interval.
        # Runs before pause check so the body stays environmentally accurate
        # even when cognition is paused.
        self._maybe_pull_weather()

        # Kill switch — if ~/.narada/heartbeat/pause exists, skip cycle
        if PAUSE_FILE.exists():
            logger.info("Paused (kill switch at %s). Skipping cycle.", PAUSE_FILE)
            self.display.set_status("paused")
            return {"action": "PAUSED", "topic": None}

        # Re-read wake manifest each cycle so edits apply immediately
        try:
            self.manifest = load_manifest()
        except WakeManifestError as e:
            logger.error("Wake manifest error, cycle aborted: %s", e)
            return {"action": "ERROR", "topic": None, "error": str(e)}

        # Load prompts referenced by the manifest
        prompts = {
            name: path.read_text(encoding="utf-8")
            for name, path in self.manifest.prompts.items()
        }

        # Resolve state variables for the desire prompt
        state = resolve_state(self.manifest)
        logger.info("State resolved (%d keys)", len(state))

        # Generate desire (local viveka)
        t_cycle_start = time.monotonic()
        logger.info("Generating desire...")
        self.display.set_status("thinking...")
        desire = self.viveka.generate_desire(
            state,
            system_prompt=prompts["system"],
            desire_template=prompts["desire"],
        )
        logger.info("Desire: %s — %s (%s)", desire.action.value, desire.topic, desire.reason)

        # Direct actions — no frontier-model delegation.
        # REST / SLEEP / CHECK_IN are handled locally so viveka's voice
        # reaches its destination unmediated.
        if not desire.needs_capability:
            if desire.action == Action.SLEEP:
                return self._handle_sleep(desire, now, t_cycle_start)
            if desire.action == Action.CHECK_IN:
                return self._handle_check_in(desire, now, t_cycle_start)
            if desire.action == Action.SPEAK:
                return self._handle_speak(desire, now, t_cycle_start)
            # REST (and any other non-capability action) — log and yield
            self.display.show_resting()
            self._log_cycle(
                CycleRecord(
                    started=now.isoformat(),
                    action=desire.action.value,
                    topic=desire.topic,
                    reason=desire.reason,
                    approved=False,
                    duration_s=time.monotonic() - t_cycle_start,
                    desire_raw=desire.raw_response,
                ),
                now=now,
            )
            logger.info("Resting.")
            return {"action": desire.action.value, "topic": None}

        # Delegate to Claude for a plan
        logger.info("Delegating to Claude...")
        self.display.show_desire(desire.action.value, desire.topic)
        plan = self.delegate.create_plan(desire)
        cycle_cost = plan.cost_usd
        logger.info("Plan received (%d chars, $%.4f)", len(plan.steps), plan.cost_usd)

        # Viveka judges the plan
        logger.info("Judging plan...")
        self.display.show_judging()
        judgment = self.viveka.judge(
            desire, plan.steps,
            system_prompt=prompts["system"],
            judgment_template=prompts["judgment"],
        )
        logger.info("Judgment: %s — %s",
                    "APPROVED" if judgment.approved else "REJECTED",
                    judgment.feedback)

        # Revise if needed
        revisions = 0
        while not judgment.approved and revisions < MAX_REVISIONS:
            revisions += 1
            logger.info("Requesting revision %d...", revisions)
            plan = self.delegate.revise_plan(plan, judgment.feedback)
            cycle_cost += plan.cost_usd
            judgment = self.viveka.judge(
                desire, plan.steps,
                system_prompt=prompts["system"],
                judgment_template=prompts["judgment"],
            )
            logger.info("Revision judgment: %s",
                        "APPROVED" if judgment.approved else "REJECTED")

        # Execute if approved
        result = None
        sandbox_violated = False
        if judgment.approved:
            logger.info("Executing approved plan...")
            self.display.show_executing(desire.topic)

            artifacts_before: set[Path] = (
                {p for p in ARTIFACTS_DIR.glob("*.md") if p.is_file()}
                if ARTIFACTS_DIR.exists() else set()
            )
            project_state_before = _project_git_head(PROJECT_GIT_ROOT)

            result = self.delegate.execute_plan(
                plan, desire,
                executor_boundaries=prompts["executor"],
            )
            cycle_cost += result.cost_usd
            logger.info("Result: %s ($%.4f)", result.summary, result.cost_usd)
            self.display.show_result(result.summary)

            # BUILD cycles are expected to change project files. For any
            # other action, a project-git-state change is a sandbox violation.
            if desire.action == Action.BUILD:
                sandbox_violated = False
            else:
                sandbox_violated = check_sandbox_violations(
                    project_state_before, PROJECT_GIT_ROOT
                )

            ingest_results = ingest_new_artifacts(artifacts_before)
            if ingest_results:
                ok_count = sum(1 for r in ingest_results if r.get("ok"))
                logger.info(
                    "Ingested %d/%d new artifacts into smriti",
                    ok_count, len(ingest_results),
                )

        logger.info("Cycle cost: $%.4f", cycle_cost)

        # Log cycle to markdown file
        self._log_cycle(
            CycleRecord(
                started=now.isoformat(),
                action=desire.action.value,
                topic=desire.topic,
                reason=desire.reason,
                approved=judgment.approved,
                revisions=revisions,
                cost_usd=cycle_cost,
                duration_s=time.monotonic() - t_cycle_start,
                desire_raw=desire.raw_response,
                plan_raw=plan.steps,
                judgment_feedback=judgment.feedback,
                result_summary=result.summary if result else "",
                result_details=result.details if result else "",
            ),
            now=now,
        )

        return {
            "action": desire.action.value,
            "topic": desire.topic,
            "approved": judgment.approved,
            "result": result.summary if result else None,
            "sandbox_violated": sandbox_violated,
        }

    def _log_cycle(self, record: CycleRecord, *, now: datetime) -> None:
        path = cycle_log_path(
            self.manifest,
            when=now,
            action=record.action,
            topic=record.topic,
        )
        try:
            write_cycle(path, record)
        except OSError as e:
            logger.error("Failed to write cycle log to %s: %s", path, e)

    # ── Direct action handlers (no frontier delegation) ────────────────

    def _handle_sleep(
        self, desire: Desire, now: datetime, t_cycle_start: float
    ) -> dict:
        """Run the smriti sleep cycle. Blocks until complete.

        v0: synchronous. While this runs, the heartbeat generates no new
        desires — the cycle loop is blocked here. The sleepy-state
        (Qwen-only responsiveness, insisted-wake with graceful stop) is
        a follow-up.
        """
        logger.info("SLEEP: launching smriti sleep --all (topic=%r)", desire.topic)
        self.display.set_status("sleeping")

        proc = subprocess.run(
            ["python", "-m", "smriti.cli", "sleep", "--all"],
            capture_output=True, text=True,
        )
        LAST_SLEEP_MARKER.parent.mkdir(parents=True, exist_ok=True)
        LAST_SLEEP_MARKER.touch()

        ok = proc.returncode == 0
        summary = (proc.stdout or "").strip().splitlines()[-1:] or [""]
        result_line = summary[0]
        logger.info("SLEEP complete (exit=%d): %s", proc.returncode, result_line)

        self._log_cycle(
            CycleRecord(
                started=now.isoformat(),
                action=desire.action.value,
                topic=desire.topic,
                reason=desire.reason,
                approved=ok,
                duration_s=time.monotonic() - t_cycle_start,
                desire_raw=desire.raw_response,
                result_summary=result_line,
                result_details=(proc.stdout or "")[-2000:],
            ),
            now=now,
        )
        return {
            "action": desire.action.value,
            "topic": desire.topic,
            "approved": ok,
            "result": result_line,
        }

    def _handle_check_in(
        self, desire: Desire, now: datetime, t_cycle_start: float
    ) -> dict:
        """Write a message to Suti and send it as email. No frontier-model
        rewrite — the content IS viveka's topic + reason.

        The message file in MESSAGES_DIR is always written (audit trail).
        If ~/.narada/heartbeat/.smtp-config exists and is valid, an email
        is also sent. Email failure is logged but does not fail the cycle.
        """
        MESSAGES_DIR.mkdir(parents=True, exist_ok=True)
        ts = now.strftime("%Y-%m-%d-%H%M%S")
        message_path = MESSAGES_DIR / f"{ts}.md"

        body = (
            f"---\n"
            f"written: {now.isoformat()}\n"
            f"topic: {desire.topic}\n"
            f"---\n\n"
            f"{desire.reason}\n"
        )
        message_path.write_text(body, encoding="utf-8")
        logger.info("CHECK_IN: wrote %s", message_path.name)
        self.display.set_status("messaged Suti")

        # Route through the action bus so the message reaches Suti
        # via body (if at PC) or Telegram (if away). Audit file above
        # is kept either way; the bus adds ACTION_INVOKE / ACTION_RESULT
        # events to the audit trail.
        from prana.bus.actions.speak import invoke_speak

        route_text = (
            f"{desire.reason}"
            if not desire.topic
            else f"{desire.topic}\n\n{desire.reason}"
        )
        route_result = invoke_speak(
            route_text,
            source="heartbeat-check-in",
            topic=desire.topic,
            priority=2,  # check-ins are higher priority than ambient speak
        )
        if route_result.ok:
            logger.info("CHECK_IN: routed to %s (uid=%d)",
                        route_result.delivered_to, route_result.utterance_id)
        else:
            logger.warning("CHECK_IN: routing failed — %s",
                           route_result.skipped_reason)

        # Also send as email (legacy path, still useful for archival).
        subject = f"[Narada] {desire.topic}" if desire.topic else "[Narada] check-in"
        email_body = (
            f"{desire.reason}\n\n"
            f"---\n"
            f"from: heartbeat CHECK_IN at {now.isoformat()}\n"
            f"audit: {message_path}\n"
        )
        email_sent = _send_check_in_email(subject, email_body)
        if email_sent:
            logger.info("CHECK_IN: email sent to Suti")
        else:
            logger.info("CHECK_IN: email not sent (config missing or send failed)")

        self._log_cycle(
            CycleRecord(
                started=now.isoformat(),
                action=desire.action.value,
                topic=desire.topic,
                reason=desire.reason,
                approved=True,
                duration_s=time.monotonic() - t_cycle_start,
                desire_raw=desire.raw_response,
                result_summary=(
                    f"message written: {message_path.name}"
                    f" | route: {route_result.delivered_to or 'failed'}"
                    f" | email: {'sent' if email_sent else 'skipped'}"
                ),
            ),
            now=now,
        )
        return {
            "action": desire.action.value,
            "topic": desire.topic,
            "approved": True,
            "result": str(message_path),
            "email_sent": email_sent,
        }

    def _handle_speak(
        self, desire: Desire, now: datetime, t_cycle_start: float
    ) -> dict:
        """Send the SPEAK content out the BOX-3 via deha's /utter endpoint.

        Like CHECK_IN, the desire.reason IS the spoken text — viveka's
        own words, not rewritten by the frontier model. Routes through
        deha's voice mediator so it serializes against other utterances
        and uses the production voice.

        Failure to reach deha is non-fatal: the cycle still completes
        and the desire is logged.
        """
        import urllib.error
        import urllib.request
        import json as _json

        text = (desire.reason or "").strip()
        if not text:
            logger.info("SPEAK: empty reason, skipping")
            self._log_cycle(
                CycleRecord(
                    started=now.isoformat(),
                    action=desire.action.value,
                    topic=desire.topic,
                    reason=desire.reason,
                    approved=False,
                    duration_s=time.monotonic() - t_cycle_start,
                    desire_raw=desire.raw_response,
                    result_summary="skipped: empty reason",
                ),
                now=now,
            )
            return {
                "action": desire.action.value,
                "topic": desire.topic,
                "approved": False,
                "result": "empty reason",
            }

        # Route through the action bus — body if Suti is at his PC,
        # Telegram if not, audited in state.db (utterance_queue + bus
        # events). The bus call is a thin wrapper over route_utterance
        # that publishes ACTION_INVOKE / ACTION_RESULT events.
        from prana.bus.actions.speak import invoke_speak

        result = invoke_speak(
            text,
            source=f"heartbeat-speak:{desire.topic[:32] or 'speak'}",
            topic=desire.topic,
            priority=1,
        )
        approved = result.ok
        if approved:
            result_summary = f"delivered_to={result.delivered_to} uid={result.utterance_id}"
            logger.info("SPEAK: %s", result_summary)
        else:
            result_summary = f"all-channels-failed: {result.skipped_reason}"
            logger.warning("SPEAK: %s", result_summary)

        self.display.set_status("spoke")
        self._log_cycle(
            CycleRecord(
                started=now.isoformat(),
                action=desire.action.value,
                topic=desire.topic,
                reason=desire.reason,
                approved=approved,
                duration_s=time.monotonic() - t_cycle_start,
                desire_raw=desire.raw_response,
                result_summary=result_summary,
            ),
            now=now,
        )
        return {
            "action": desire.action.value,
            "topic": desire.topic,
            "approved": approved,
            "result": result_summary,
        }

    def run(self) -> None:
        """Run the heartbeat loop until stopped."""
        self._running = True

        def handle_signal(signum, frame):
            logger.info("Received signal %d, stopping heartbeat...", signum)
            self._running = False

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

        logger.info("Heartbeat daemon starting (interval: %ds)", self.interval)
        logger.info("Loading viveka core...")
        self.viveka.load()

        while self._running:
            try:
                self.beat_once()
            except Exception as exc:
                logger.exception("Heartbeat error: %s", exc)

            if not self._running:
                break

            logger.info("Sleeping %ds until next heartbeat...", self.interval)
            for _ in range(self.interval):
                if not self._running:
                    break
                time.sleep(1)

        self.viveka.unload()
        logger.info("Heartbeat daemon stopped.")
