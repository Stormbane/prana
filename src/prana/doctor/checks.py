"""The checks. Each is `check_x(p) -> CheckResult`, reads only through
the Probes boundary, and judges one piece of Narada's home system.

Liveness is not health (the lesson of 2026-11-03): the supervisor said
every component was healthy while the BOX worker bailed on every job
and the brain 500'd every Telegram turn. So checks here look for the
*function* — an agent actually in the BOX's room, a resumable brain
session, a nightly that finished every step — not just a port.
"""
from __future__ import annotations

import json
import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import yaml

from prana.doctor.model import AMBER, GREEN, RED, CheckResult, worst
from prana.doctor.probes import Probes, _parse_iso

DEVICE_ROOM = "narada-body"
DEVICE_IDENTITY = "narada-box3"
BOX_HEALTH = "http://127.0.0.1:8792/"
PHONE_HEALTH = "http://127.0.0.1:8796/"
BRAIN_HEALTH = "http://127.0.0.1:8811/health"
LIVEKIT_LOCAL = "http://127.0.0.1:7880/"
HOST_LOG_TAIL = 3 * 1024 * 1024
# Where the 2026-10-22 disk cleanup archived the wake model (audit
# 2026-11-03). Reported as a restore hint when present; never copied.
WAKE_ARCHIVE_CANDIDATES = (Path("D:/Archive/wakeword/output/narada/narada.onnx"),)


# ── helpers ───────────────────────────────────────────────────────────

def _json(text: str | None):
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _local(p: Probes, dt: datetime | None) -> str:
    if dt is None:
        return "unknown"
    return dt.astimezone(p.local_tz).strftime("%Y-%m-%d %H:%M")


def _ago(p: Probes, dt: datetime | None) -> str:
    if dt is None:
        return "never"
    secs = int((p.now() - dt).total_seconds())
    if secs < 0:
        return "just now"
    if secs < 120:
        return f"{secs}s ago"
    mins = secs // 60
    if mins < 120:
        return f"{mins} min ago"
    hours = mins // 60
    if hours < 48:
        return f"{hours}h ago"
    return f"{hours // 24} days ago"


_HOST_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ")


def _host_ts(p: Probes, line: str) -> datetime | None:
    m = _HOST_TS.match(line)
    if not m:
        return None
    naive = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    return naive.replace(tzinfo=p.local_tz)


def _parse_local_iso(p: Probes, value) -> datetime | None:
    """ISO 8601; a naive stamp is the machine's local time."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=p.local_tz)


def _alert_rows(p: Probes) -> dict[str, dict]:
    try:
        rows = p.sqlite_query(p.paths.state_db,
                              "SELECT * FROM host_alert_status")
    except Exception:  # noqa: BLE001
        return {}
    return {r["component"]: r for r in rows}


def _epoch(value) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(float(value), timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _cron_runs(text: str) -> list[dict]:
    """Parse cron_run.py's per-job log: `=== ts run: ... ===`, output,
    `=== ts exit rc=N ===`."""
    runs: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        m = re.match(r"^=== (\S+) run: (.*?)( ===)?$", line)
        if m:
            cur = {"start": _parse_iso(m.group(1)), "rc": None, "end": None,
                   "out": []}
            runs.append(cur)
            continue
        m = re.match(r"^=== (\S+) exit rc=(-?\d+) ===", line)
        if m and cur is not None:
            cur["end"] = _parse_iso(m.group(1))
            cur["rc"] = int(m.group(2))
            continue
        if cur is not None and line.strip():
            cur["out"].append(line.strip())
    return runs


def _nearest_existing_ancestor(p: Probes, path: Path) -> Path | None:
    for parent in path.parents:
        if p.exists(parent):
            return parent
    return None


def _voice_fault(p: Probes) -> dict | None:
    fault = _json(p.read_text(p.paths.voice_fault))
    return fault if isinstance(fault, dict) and fault.get("code") else None


def _resume_state(p: Probes, tier: str, session: str) -> tuple[str | None, bool | None]:
    """(native id, transcript present?) for a brain session. Present is
    None when there's nothing to resume (no id, or a non-Claude backend)."""
    d = p.paths.brain_sessions / tier / session
    native = (p.read_text(d / "native-session-id") or "").strip()
    backend = (p.read_text(d / "backend") or "claude").strip() or "claude"
    if not native or backend != "claude":
        return (native or None), None
    hits = p.glob(p.paths.claude_projects, f"*/{native}.jsonl")
    return native, bool(hits)


def _result(cid: str, title: str) -> Callable[..., CheckResult]:
    def make(status: str, reason: str, **kw) -> CheckResult:
        return CheckResult(id=cid, title=title, status=status, reason=reason, **kw)
    return make


# ── supervisor ────────────────────────────────────────────────────────

def check_host(p: Probes) -> CheckResult:
    r = _result("host", "Supervisor (Narada_Host)")
    lock = _json(p.read_text(p.paths.host_lock)) or {}
    since = _parse_iso(lock.get("start_time"))
    details: dict = {}
    # The beacon decides; Task Scheduler (a slow PowerShell call) is only
    # consulted when there is no beacon to explain.
    hb = _json(p.read_text(p.paths.heartbeat))
    if not isinstance(hb, dict) or "ts" not in hb:
        task = p.scheduled_tasks().get("Narada_Host")
        details = {"task_last_result": task.last_result if task else None,
                   "task_last_run": task.last_run.isoformat() if task and task.last_run else None}
        return r(RED, f"no heartbeat beacon at {p.paths.heartbeat}",
                 hint="start the Narada_Host task; the deadman should do this within 5 min",
                 details=details)
    beat = _epoch(hb["ts"])
    age = (p.now() - beat).total_seconds()
    pid = hb.get("pid")
    details.update(pid=pid, beacon_age_s=int(age))
    if pid is None or not p.pid_alive(pid):
        return r(RED, f"host pid {pid} is not running (last beat {_ago(p, beat)})",
                 since=beat, hint="the deadman restarts Narada_Host when the beat is >5 min stale",
                 details=details)
    if age > 300:
        return r(RED, f"heartbeat stale: last beat {_ago(p, beat)} (pid {pid} hung?)",
                 since=beat, details=details)
    if age > 90:
        return r(AMBER, f"heartbeat late: last beat {_ago(p, beat)}", since=beat,
                 details=details)
    return r(GREEN, f"pid {pid} beating (last {_ago(p, beat)}), up since {_local(p, since)}",
             since=since, details=details)


def check_components(p: Probes) -> CheckResult:
    r = _result("components", "Supervised components")
    cfg = yaml.safe_load(p.read_text(p.paths.components_yaml) or "") or {}
    # A component without `enabled:` is enabled, as the host reads it
    # (host/component.py).
    enabled = [c for c in cfg.get("components", []) if c.get("enabled", True)]
    if not enabled:
        return r(RED, f"no enabled components in {p.paths.components_yaml}")
    alerts = _alert_rows(p)
    down, up, unprobed = [], [], []
    for c in enabled:
        url = c.get("health_url")
        if not url:
            unprobed.append(c["name"])
            continue
        res = p.http_get(url, timeout=4)
        if res.ok:
            up.append(c["name"])
        else:
            down.append(f"{c['name']} ({res.status or res.error or 'no answer'})")
    details = {"up": up, "down": down, "liveness_only": unprobed}
    if down:
        opened = [_epoch(alerts.get(n.split(" ")[0], {}).get("episode_opened_at"))
                  for n in down]
        opened = [o for o in opened if o]
        return r(RED, "down: " + ", ".join(down), since=min(opened) if opened else None,
                 hint="see host.log for the component; the supervisor restarts it",
                 details=details)
    healthy = [_epoch(alerts.get(n, {}).get("healthy_since")) for n in up]
    healthy = [h for h in healthy if h]
    extra = f"; {len(unprobed)} without a probe ({', '.join(unprobed)})" if unprobed else ""
    return r(GREEN, f"{len(up)} answering their health URL{extra}",
             since=max(healthy) if healthy else None, details=details)


# ── brain ─────────────────────────────────────────────────────────────

def check_brain_local(p: Probes) -> CheckResult:
    r = _result("brain_local", "Brain :8811")
    since = _epoch(_alert_rows(p).get("brain", {}).get("healthy_since"))
    res = p.http_get(BRAIN_HEALTH, timeout=4)
    body = _json(res.body) if res.ok else None
    if not res.ok or not isinstance(body, dict) or not body.get("ok"):
        return r(RED, f"/health failed ({res.status or res.error})",
                 hint="brain component down: check host.log [brain]")
    return r(GREEN, f"/health ok; {body.get('sessions', '?')} warm session(s), "
                    f"{body.get('busy', '?')} busy", since=since, details=body)


def check_brain_tailnet(p: Probes) -> CheckResult:
    r = _result("brain_tailnet", "Brain over tailnet :8443")
    if not p.tailnet_base:
        return r(AMBER, "tailnet URL not configured (NARADA_DOCTOR_TAILNET)")
    url = f"{p.tailnet_base}:8443/health"
    res = p.http_get(url, timeout=6)
    if res.ok:
        return r(GREEN, f"{url} answers", details={"url": url})
    return r(RED, f"{url} unreachable ({res.status or res.error})",
             hint="tailscale serve on 8443 (prana/deploy/tailscale-serve.ps1)",
             details={"url": url})


def check_brain_sessions(p: Probes) -> CheckResult:
    """Information, not a fault. Claude Code prunes transcripts after
    cleanupPeriodDays (default 30); since prana 51a3f27 the brain notices
    a pruned resume target and starts that session fresh with a handover
    from its own transcript, so a pruned id costs continuity, not a turn."""
    r = _result("brain_sessions", "Brain sessions")
    stale, checked = [], 0
    for tier in p.listdir(p.paths.brain_sessions):
        for name in p.listdir(p.paths.brain_sessions / tier):
            native, present = _resume_state(p, tier, name)
            if present is None:
                continue
            checked += 1
            if not present:
                stale.append(f"{tier}/{name}")
    if stale:
        return r(GREEN, f"{checked} resumable session(s); {len(stale)} will start fresh with a "
                        f"handover on next use (transcript expired): {', '.join(stale[:5])}",
                 details={"expired": stale, "checked": checked})
    return r(GREEN, f"{checked} resumable session(s), all transcripts present",
             details={"checked": checked})


# ── telegram ──────────────────────────────────────────────────────────

def check_telegram_bridge(p: Probes) -> CheckResult:
    r = _result("telegram_bridge", "Telegram bridge polling")
    log = p.tail_text(p.paths.host_log, HOST_LOG_TAIL) or ""
    polls_ok, polls_bad, conflicts = [], [], []
    for line in log.splitlines():
        if "[chat-bridge]" not in line:
            continue
        ts = _host_ts(p, line)
        if "Conflict" in line or "terminated by other getUpdates" in line:
            if ts is not None:
                conflicts.append(ts)
            continue
        if "getUpdates" in line and ts is not None:
            (polls_ok if "200 OK" in line else polls_bad).append(ts)
    since = _epoch(_alert_rows(p).get("chat-bridge", {}).get("healthy_since"))
    details = {"polls_ok": len(polls_ok), "polls_not_ok": len(polls_bad),
               "conflicts": len(conflicts)}
    # A restart leaves one Conflict line while the old poll drains; only a
    # conflict with no good poll after it means a second poller now.
    if conflicts and (not polls_ok or conflicts[-1] >= polls_ok[-1]):
        return r(RED, "getUpdates conflict: another process is polling this bot token",
                 since=conflicts[-1],
                 hint="only one poller per token; find the second bridge", details=details)
    if not polls_ok and not polls_bad:
        # The poll lines come from httpx's INFO request logging, which
        # log_hygiene asks to turn off (they carry the token). Without
        # them this check can't see polling; it doesn't call that red.
        return r(AMBER, "no getUpdates lines in the recent host.log (request logging off?); "
                        "polling not visible here",
                 hint="the components check still covers the bridge's liveness",
                 details=details)
    if not polls_ok:
        return r(RED, f"no successful getUpdates in the recent host.log "
                      f"({len(polls_bad)} failed)",
                 hint="chat-bridge component: check host.log", details=details)
    last = polls_ok[-1]
    age = (p.now() - last).total_seconds()
    details["last_poll"] = last.isoformat()
    if age > 900:
        return r(RED, f"last successful poll {_ago(p, last)}", since=last, details=details)
    if age > 120:
        return r(AMBER, f"last successful poll {_ago(p, last)}", since=last, details=details)
    return r(GREEN, f"long-polling getUpdates (last {_ago(p, last)}; "
                    f"{len(polls_ok)} ok, {len(polls_bad)} failed in window)",
             since=since, details=details)


_CYCLE = re.compile(r"^(\d{4}-\d{2}-\d{2}-\d{6})-chat-(-?\d+)\.md$")


def check_telegram_inbound(p: Probes) -> CheckResult:
    r = _result("telegram_inbound", "Telegram inbound → reply")
    exchanges = []
    for name in p.listdir(p.paths.chat_cycles):
        m = _CYCLE.match(name)
        if not m:
            continue
        ts = datetime.strptime(m.group(1), "%Y-%m-%d-%H%M%S").replace(tzinfo=timezone.utc)
        exchanges.append((ts, m.group(2), name))
    if not exchanges:
        return r(AMBER, "no Telegram exchanges recorded")
    exchanges.sort()

    def status_of(name: str) -> tuple[str, str]:
        text = p.read_text(p.paths.chat_cycles / name) or ""
        ok = "FAILED" not in text.split("\n", 1)[0]
        reply = text.split("## Narada", 1)[-1].strip().splitlines()
        return ("OK" if ok else "FAILED"), (reply[0][:120] if reply else "")

    last_ts, chat_id, last_name = exchanges[-1]
    last_status, last_reply = status_of(last_name)
    # Walk back to the last OK; the failure streak starts just after it.
    last_ok, streak_start = None, None
    for ts, _cid, name in reversed(exchanges):
        st, _ = status_of(name)
        if st == "OK":
            last_ok = ts
            break
        streak_start = ts
    gen = (p.read_text(p.paths.chat_sessions / chat_id / ".brain-generation") or "0").strip()
    session = f"telegram-{chat_id}" if gen in ("", "0") else f"telegram-{chat_id}-g{gen}"
    native, present = _resume_state(p, "prana", session)
    details = {"last_inbound": last_ts.isoformat(), "last_status": last_status,
               "last_ok": last_ok.isoformat() if last_ok else None,
               "brain_session": session,
               "resume_target": native[:8] if native else None,
               "resume_target_present": present}
    # A pruned resume target is no longer a fault: the brain starts fresh
    # with a handover (prana 51a3f27). It stays in details for context.
    if last_status == "FAILED":
        failed = f"last exchange {_local(p, last_ts)} FAILED {last_reply}".strip()
        hint = (f"read host.log [brain] around {_local(p, last_ts)}; "
                "/new in Telegram starts a fresh brain session")
        brain_up = _epoch(_alert_rows(p).get("brain", {}).get("healthy_since"))
        details["brain_healthy_since"] = brain_up.isoformat() if brain_up else None
        if brain_up and brain_up > last_ts:
            # Restarted (perhaps fixed) since, and untested: not known red.
            return r(AMBER, f"{failed}; the brain restarted at {_local(p, brain_up)} and no "
                            "message has tested it since",
                     since=streak_start or last_ts, hint=hint, details=details)
        return r(RED, failed, since=streak_start or last_ts, hint=hint, details=details)
    return r(GREEN, f"last inbound {_local(p, last_ts)} ({_ago(p, last_ts)}) answered OK",
             since=last_ok, details=details)


# ── voice / LiveKit / BOX ─────────────────────────────────────────────

def check_livekit(p: Probes) -> CheckResult:
    r = _result("livekit", "LiveKit media server")
    local = p.http_get(LIVEKIT_LOCAL, timeout=4)
    since = _epoch(_alert_rows(p).get("livekit", {}).get("healthy_since"))
    if not local.ok:
        return r(RED, f"local :7880 down ({local.status or local.error})",
                 hint="livekit component: check host.log [livekit]")
    if not p.tailnet_base:
        return r(GREEN, "local :7880 answers (tailnet not checked)", since=since)
    wss = f"{p.tailnet_base}:7443/"
    remote = p.http_get(wss, timeout=6)
    if not remote.ok:
        return r(AMBER, f"LAN ok, but {wss} unreachable ({remote.status or remote.error}): "
                        "the phone can't join", since=since)
    return r(GREEN, "local :7880 and tailnet wss :7443 answer", since=since)


def _box_log_stats(p: Probes, log: str) -> dict:
    jobs = {"ok": 0, "failed": 0}
    wake_errors: list[datetime] = []
    recycles: list[datetime] = []
    joins: list[datetime] = []
    job_re = re.compile(r'job ended\s+\{.*?"agentName": "([^"]*)".*?"status": "(JS_\w+)"')
    for line in log.splitlines():
        if "participant active" in line and f'"participant": "{DEVICE_IDENTITY}"' in line:
            ts = _host_ts(p, line)
            if ts:
                joins.append(ts)
        elif "job ended" in line:
            m = job_re.search(line)
            if m and m.group(1) == "":  # the BOX worker registers with no agent name
                jobs["ok" if m.group(2) == "JS_SUCCESS" else "failed"] += 1
        elif "[voice]: err: ERROR:narada-voice:wake model unavailable" in line:
            ts = _host_ts(p, line)
            if ts:
                wake_errors.append(ts)
        elif "[voice]: err:" in line and "orphaned (device present, no agent)" in line:
            ts = _host_ts(p, line)
            if ts:
                recycles.append(ts)
    gaps = [(b - a).total_seconds() for a, b in zip(recycles, recycles[1:])]
    return {"jobs": jobs, "wake_errors": wake_errors, "recycles": recycles, "joins": joins,
            "recycle_every_s": int(statistics.median(gaps)) if gaps else None}


def check_box_dispatch(p: Probes) -> CheckResult:
    r = _result("box_dispatch", "BOX voice: agent dispatch")
    stats = _box_log_stats(p, p.tail_text(p.paths.host_log, HOST_LOG_TAIL) or "")
    jobs = stats["jobs"]
    details = {"jobs_ok": jobs["ok"], "jobs_failed": jobs["failed"],
               "wake_model_errors": len(stats["wake_errors"]),
               "room_recycles": len(stats["recycles"]),
               "recycle_every_s": stats["recycle_every_s"]}
    total = jobs["ok"] + jobs["failed"]
    every = f" every ~{stats['recycle_every_s']}s" if stats["recycle_every_s"] else ""

    def bail_loop() -> CheckResult:
        # A bail ends the job as JS_SUCCESS or JS_FAILED alike, so the
        # bail count is the signal. "Since": when the model went missing
        # (its nearest surviving directory's mtime) beats log retention.
        since = stats["wake_errors"][0]
        if not p.exists(p.paths.wake_model):
            anc = _nearest_existing_ancestor(p, p.paths.wake_model)
            gone = p.mtime(anc) if anc else None
            if gone and gone < since:
                since = gone
        return r(RED, f"BOX joins, no agent stays: {len(stats['wake_errors'])} of {total} "
                      "recent jobs bailed 'wake model unavailable' before tap admission, so "
                      f"taps are ignored and the room is recycled{every} (the black dog)",
                 since=since, since_approx=True,
                 hint="restore narada.onnx (see voice_box) — the worker then admits taps",
                 details=details)

    # The bail loop is the verdict whatever instant LiveKit is caught in
    # (mid-recycle the room is briefly absent): judge it from the log.
    recent_bail = bool(stats["wake_errors"]) and \
        p.now() - stats["wake_errors"][-1] < timedelta(minutes=10)
    try:
        parts = p.livekit_participants(DEVICE_ROOM)
    except RuntimeError as exc:
        if recent_bail:
            return bail_loop()
        return r(AMBER, f"can't query LiveKit ({exc})", details=details)
    device = parts is not None and any(x["identity"] == DEVICE_IDENTITY for x in parts)
    agent = parts is not None and any(x["kind"] == "AGENT" for x in parts)
    details.update(device_in_room=device, agent_in_room=agent)
    if device and agent:
        return r(GREEN, f"agent in the room with the BOX; recent jobs {jobs['ok']} ok / "
                        f"{jobs['failed']} failed", details=details)
    if recent_bail or (device and stats["wake_errors"]):
        return bail_loop()
    if not device:
        return r(AMBER, f"{DEVICE_ROOM} has no BOX participant right now (see box_device)",
                 details=details)
    if jobs["failed"] or len(stats["recycles"]) >= 2:
        return r(RED, f"BOX in room, no agent: {jobs['failed']}/{total} recent jobs failed, "
                      f"{len(stats['recycles'])} room recycles{every}",
                 since=stats["recycles"][0] if stats["recycles"] else None,
                 since_approx=True, hint="read host.log [voice] around a 'job ended'",
                 details=details)
    return r(AMBER, "BOX in room, no agent right now (dispatch may be in flight)",
             details=details)


def _last_billed_day(p: Probes) -> str | None:
    budget = _json(p.read_text(p.paths.voice_budget)) or {}
    days = sorted((budget.get("days") or {}).keys())
    return days[-1] if days else None


def _voice_backend_problems(p: Probes) -> tuple[list[str], list[str], list[datetime]]:
    red, amber, since = [], [], []
    fault = _voice_fault(p)
    if fault:
        at = _parse_local_iso(p, fault.get("at"))  # the fault file stamps local time
        # Only a good BOX session removes the file (worker.py). A later
        # budget entry proves nothing: the phone worker settles the budget
        # even when the provider refuses to start (Codex review,
        # 2026-11-04), and on 11-04 it was still reporting empty credit.
        if fault["code"] == "credits":
            red.append(f"OpenAI credit empty (fault last recorded {_local(p, at)})")
        else:
            amber.append(f"last voice fault '{fault['code']}' at {_local(p, at)}")
        if at:
            since.append(at)
    return red, amber, since


def check_voice_box(p: Probes) -> CheckResult:
    r = _result("voice_box", "BOX voice worker")
    red, amber, since = [], [], []
    details: dict = {}
    health = p.http_get(BOX_HEALTH, timeout=4)
    if not health.ok:
        red.append(f"worker health :8792 down ({health.status or health.error})")
    model = p.paths.wake_model
    if not p.exists(model):
        red.append(f"wake model missing ({model}): every BOX job bails before tap admission")
        anc = _nearest_existing_ancestor(p, model)
        gone = p.mtime(anc) if anc else None
        if gone:
            since.append(gone)
        archived = [str(c) for c in WAKE_ARCHIVE_CANDIDATES if p.exists(c)]
        details["wake_model_archive_copy"] = archived or None
    b_red, b_amber, b_since = _voice_backend_problems(p)
    red += b_red
    amber += b_amber
    since += b_since
    details["last_billed_day"] = _last_billed_day(p)
    hint = ""
    if details.get("wake_model_archive_copy"):
        hint = (f"copy {details['wake_model_archive_copy'][0]} back to {model}; "
                "voice still needs a funded backend")
    elif not p.exists(model):
        hint = "restore the wake model (or NARADA_WAKE_GATING=off)"
    elif b_red:
        hint = "fund the voice backend, or move voice off it (presence plan W5)"
    status = RED if red else AMBER if amber else GREEN
    reason = "; ".join(red + amber) or "worker up, wake model present, no voice fault"
    if details["last_billed_day"]:
        reason += f" (last billed session {details['last_billed_day']})"
    return r(status, reason, since=min(since) if since and status != GREEN else None,
             since_approx=bool(since), hint=hint, details=details)


def check_voice_phone(p: Probes) -> CheckResult:
    r = _result("voice_phone", "Phone voice worker")
    health = p.http_get(PHONE_HEALTH, timeout=4)
    if not health.ok:
        return r(RED, f"worker health :8796 down ({health.status or health.error})")
    b_red, b_amber, b_since = _voice_backend_problems(p)
    if b_red:
        return r(RED, "registered, but " + "; ".join(b_red) + " (shared voice backend)",
                 since=min(b_since) if b_since else None, since_approx=True,
                 hint="fund the voice backend, or move voice off it (presence plan W5)")
    if b_amber:
        return r(AMBER, "registered; " + "; ".join(b_amber))
    return r(GREEN, "registered (liveness only; dispatch is explicit and untested here)")


def check_box_device(p: Probes) -> CheckResult:
    r = _result("box_device", "BOX device (ESP32-S3-BOX-3)")
    try:
        parts = p.livekit_participants(DEVICE_ROOM)
    except RuntimeError as exc:
        return r(AMBER, f"can't see the BOX: {exc}")
    present = bool(parts) and any(x["identity"] == DEVICE_IDENTITY for x in parts)
    try:
        cutoff = (p.now() - timedelta(hours=24)).timestamp()
        events = p.sqlite_query(
            p.paths.state_db,
            "SELECT kind, at FROM host_alert_events WHERE component = 'box3-device' "
            "AND at > ? ORDER BY at", (cutoff,))
    except Exception:  # noqa: BLE001
        events = []
    drops = [e for e in events if e["kind"] == "health-fail"]
    details = {"present": present, "drops_24h": len(drops)}
    if not present:
        joins = _box_log_stats(p, p.tail_text(p.paths.host_log, HOST_LOG_TAIL) or "")["joins"]
        if joins and p.now() - joins[-1] < timedelta(minutes=3):
            return r(AMBER, f"between room recycles (last joined {_ago(p, joins[-1])}); "
                            "see box_dispatch", details=details)
        last = _epoch(drops[-1]["at"]) if drops else None
        return r(RED, f"BOX not in the {DEVICE_ROOM} room", since=last, since_approx=True,
                 hint="power-cycle the BOX; check Wi-Fi", details=details)
    if len(drops) >= 3:
        return r(AMBER, f"connected, but dropped {len(drops)}× in 24h "
                        "(room recycles; see box_dispatch)", details=details)
    return r(GREEN, f"connected to {DEVICE_ROOM}", details=details)


# ── phone push ────────────────────────────────────────────────────────

def check_push_subs(p: Probes) -> CheckResult:
    r = _result("push_subs", "Phone push subscriptions")
    raw = p.read_text(p.paths.push_subs)
    data = _json(raw)
    if raw is None or data is None:
        return r(RED, f"{p.paths.push_subs} missing or unreadable")
    n = len(data) if isinstance(data, (dict, list)) else 0
    if n == 0:
        return r(RED, "no devices subscribed: pushes reach nobody",
                 since=p.mtime(p.paths.push_subs), since_approx=True,
                 hint="in the app: Settings, Notifications, Enable",
                 details={"devices": 0})
    return r(GREEN, f"{n} device(s) subscribed", details={"devices": n})


# ── smriti ────────────────────────────────────────────────────────────

def _job_watch_streak(p: Probes, task: str) -> datetime | None:
    """First FAILED job-watch line after the last ok line for `task` in
    the deadman log: when the current failure streak began (±5 min)."""
    text = p.tail_text(p.paths.deadman_log, 16 * 1024 * 1024) or ""
    streak = None
    for line in text.splitlines():
        if f"job-watch: task '{task}'" in line and "FAILED" in line:
            if streak is None:
                streak = _parse_iso(line.split(" ", 1)[0])
        elif f"job-watch: {task} ok" in line:
            streak = None
    return streak


def check_smriti_nightly(p: Probes) -> CheckResult:
    r = _result("smriti_nightly", "smriti nightly")
    st = _json(p.read_text(p.paths.nightly_status))
    if not isinstance(st, dict):
        return r(RED, f"no nightly status at {p.paths.nightly_status}")
    started = _parse_iso(st.get("started"))
    now_local = p.now().astimezone(p.local_tz)
    run_at = now_local.replace(hour=3, minute=0, second=0, microsecond=0)
    if now_local < run_at + timedelta(minutes=45):
        run_at -= timedelta(days=1)
    target = st.get("target_day", "?")
    steps = st.get("steps") or {}
    failed = [k for k, v in steps.items() if isinstance(v, dict) and v.get("ok") is False]
    details = {"target_day": target, "started": st.get("started"),
               "failed_steps": failed, "steps": list(steps)}
    if started is None or started < run_at - timedelta(hours=1):
        return r(RED, f"nightly hasn't run since {_local(p, started)}",
                 since=started, hint="Task Scheduler: smriti-nightly", details=details)
    if failed or st.get("ok") is False:
        first = steps.get(failed[0], {}) if failed else {}
        err = str(first.get("error", "")).split(". ")[0][:140]
        streak = _job_watch_streak(p, "smriti-nightly")
        return r(RED, f"last night ({target}) failed at step "
                      f"{', '.join(failed) or '?'}: {err}".rstrip(": "),
                 since=streak or started, since_approx=streak is not None,
                 hint=f"the full error is in {p.paths.nightly_status}; rerun by hand with "
                      "`smriti nightly` to watch the step", details=details)
    return r(GREEN, f"last night ({target}) ok: {len(steps)} steps", since=started,
             details=details)


def check_smriti_tasks(p: Probes) -> CheckResult:
    r = _result("smriti_tasks", "smriti logd")
    tasks = p.scheduled_tasks()
    red, amber = [], []
    pid_text = (p.read_text(p.paths.logd_pid) or "").strip()
    pid = int(pid_text) if pid_text.isdigit() else None
    if pid is None or not p.pid_alive(pid):
        red.append(f"logd daemon not running (pid {pid})")
    state_m = p.mtime(p.paths.logd_state)
    if state_m is None or (p.now() - state_m) > timedelta(minutes=30):
        amber.append(f"logd state last written {_ago(p, state_m)}")
    logd = tasks.get("smriti-logd")
    if logd and logd.last_result not in (0, None, 0x41301):
        amber.append(f"smriti-logd task last result 0x{logd.last_result:X}")
    status = RED if red else AMBER if amber else GREEN
    reason = "; ".join(red + amber) or f"logd alive (pid {pid})"
    return r(status, reason, details={"logd_pid": pid})


MORNING_DUE = (7, 30)  # local; smriti-morning runs at 07:00


def check_morning(p: Probes) -> CheckResult:
    """The morning message is the heartbeat Suti notices: if it doesn't
    come, he knows something is wrong without any extra service. This
    check says which day it is for and, when it failed, at which phase
    (smriti daylog/morning.py writes `phase` and an exception type)."""
    r = _result("morning", "Morning message (the daily heartbeat)")
    now_local = p.now().astimezone(p.local_tz)
    due = now_local.hour * 60 + now_local.minute >= MORNING_DUE[0] * 60 + MORNING_DUE[1]
    want = (now_local.date() if due else (now_local - timedelta(days=1)).date()).isoformat()
    ms = _json(p.read_text(p.paths.morning_status)) or {}
    details = {"want": want, "day": ms.get("day"), "phase": ms.get("phase"),
               "reason": ms.get("reason")}
    task = p.scheduled_tasks().get("smriti-morning")
    task_note = (f" (smriti-morning task last result 0x{task.last_result:X})"
                 if task and task.last_result not in (0, None, 0x41301) else "")
    if ms.get("day") != want:
        return r(RED, f"no morning message for {want} (last {ms.get('day', 'never')})"
                      + task_note,
                 hint="Task Scheduler: smriti-morning; run `smriti morning` to see why",
                 details=details)
    if not ms.get("sent"):
        phase = ms.get("phase")
        what = {"compose": "composing failed", "deliver": "composed, but delivery failed"}.get(
            phase, "not sent")
        why = ms.get("reason") or "no reason recorded"
        return r(RED, f"morning for {want}: {what} ({why})" + task_note,
                 hint="delivery goes through notify_suti; check the System log's channel results"
                 if phase == "deliver" else "run `smriti morning` to see the error",
                 details=details)
    return r(GREEN, f"sent for {want} ({ms.get('composed_chars', '?')} chars)" + task_note,
             details=details)


# ── akhada ────────────────────────────────────────────────────────────

def check_akhada_tick(p: Probes) -> CheckResult:
    r = _result("akhada_tick", "akhada check-in tick")
    runs = _cron_runs(p.tail_text(p.paths.cron_logs / "Narada_Akhada_Checkin.log",
                                  128 * 1024) or "")
    done = [x for x in runs if x["rc"] is not None]
    if not done:
        return r(RED, "no completed tick in the cron log")
    last = done[-1]
    when = last["end"] or last["start"]
    channel = next((ln for ln in last["out"] if ln.startswith(("nudge sent", "nothing"))), "")
    details = {"last_rc": last["rc"], "last_run": when.isoformat() if when else None,
               "output": [ln[:160] for ln in last["out"][-3:]]}
    if last["rc"] != 0:
        return r(RED, f"last tick {_local(p, when)} exited rc={last['rc']}", since=when,
                 details=details)
    if when is None or (p.now() - when) > timedelta(hours=14):
        return r(RED, f"no tick for {_ago(p, when)} (runs 11:00, 18:00, 22:00)",
                 since=when, details=details)
    sent = channel.split(":")[0] if channel else "ran"
    return r(GREEN, f"last tick {_local(p, when)} ok ({sent})", since=when, details=details)


# ── memory backup ─────────────────────────────────────────────────────

def check_memory_backup(p: Probes) -> CheckResult:
    r = _result("memory_backup", "Memory backup pushed (GitHub)")
    repo = p.paths.narada
    upstream = p.git(repo, "rev-parse", "--abbrev-ref", "@{u}")
    if upstream.rc != 0:
        return r(RED, f"~/.narada has no upstream ({(upstream.err or 'git failed').strip()[:100]})")
    count = p.git(repo, "rev-list", "--count", "@{u}..HEAD")
    if count.rc != 0 or not count.out.strip().isdigit():
        return r(AMBER, f"can't count unpushed commits ({count.err.strip()[:100]})")
    n = int(count.out.strip())
    ref = "refs/remotes/" + upstream.out.strip()
    reflog = p.git(repo, "log", "-g", "-1", "--date=iso-strict", "--format=%gd%x09%gs", ref)
    m = re.search(r"@\{([^}]+)\}", reflog.out or "")
    remote_at = _parse_iso(m.group(1)) if m else None
    details = {"unpushed": n, "upstream": upstream.out.strip(),
               "remote_ref_updated": remote_at.isoformat() if remote_at else None,
               "note": "as of the last push/fetch; the doctor never fetches"}
    if n == 0:
        return r(GREEN, f"all commits pushed (remote ref updated {_ago(p, remote_at)})",
                 since=remote_at, details=details)
    dates = p.git(repo, "log", "@{u}..HEAD", "--format=%cI")
    stamps = [_parse_iso(x) for x in dates.out.split()] if dates.rc == 0 else []
    oldest = min((s for s in stamps if s), default=None)
    details["oldest_unpushed"] = oldest.isoformat() if oldest else None
    age_h = (p.now() - oldest).total_seconds() / 3600 if oldest else 0
    reason = f"{n} commit(s) unpushed, oldest {_local(p, oldest)} ({_ago(p, oldest)})"
    hint = "git -C ~/.narada push (backup.py swallows push errors: exit 0, stderr only)"
    if age_h > 48 or n >= 50:
        return r(RED, reason, since=oldest, hint=hint, details=details)
    if age_h > 12:
        return r(AMBER, reason, since=oldest, hint=hint, details=details)
    return r(GREEN, reason, since=oldest, details=details)


# ── watchdogs ─────────────────────────────────────────────────────────

def check_deadman(p: Probes) -> CheckResult:
    r = _result("deadman", "Dead-man watchdog (deadman.ps1)")
    # job-watch.ps1 shares deadman.log (the same task's second action);
    # its lines say nothing about whether deadman.ps1 itself ran.
    tail = [ln for ln in (p.tail_text(p.paths.deadman_log, 16 * 1024) or "").strip().splitlines()
            if " job-watch: " not in ln]
    last_line = tail[-1] if tail else ""
    last = _parse_iso(last_line.split(" ", 1)[0]) if last_line else None
    state = _json(p.read_text(p.paths.deadman_state)) or {}
    restarts = [_epoch(x) if not isinstance(x, str) else _parse_iso(x)
                for x in state.get("restarts") or []]
    recent = [x for x in restarts if x and p.now() - x < timedelta(hours=1)]
    task = p.scheduled_tasks().get("Narada_Deadman")
    can_page = p.exists(p.paths.home / ".hermes" / ".env")
    details = {"last_line": last_line[:160], "restarts_last_hour": len(recent),
               "paged_exhausted": bool(state.get("paged_exhausted")),
               "can_page": can_page,
               "task_last_result": task.last_result if task else None}
    if last is None or p.now() - last > timedelta(minutes=15):
        return r(RED, f"deadman silent: last log line {_ago(p, last)}", since=last,
                 hint="Task Scheduler: Narada_Deadman (every 5 min)", details=details)
    if state.get("paged_exhausted"):
        return r(RED, "restart budget exhausted; deadman is standing down",
                 since=max(recent) if recent else None, details=details)
    amber = []
    if recent:
        amber.append(f"{len(recent)} host restart(s) in the last hour")
    if not can_page:
        amber.append("can't page: ~/.hermes/.env (bot token) missing")
    if task and task.last_result not in (0, None, 0x41301):
        amber.append(f"task last result 0x{task.last_result:X}")
    if p.now() - last > timedelta(minutes=7):
        amber.append(f"last tick {_ago(p, last)}")
    if amber:
        return r(AMBER, "; ".join(amber), details=details)
    return r(GREEN, f"ticking every 5 min (last {_ago(p, last)}); no restarts in the last hour",
             details=details)


def check_host_alerts(p: Probes) -> CheckResult:
    r = _result("host_alerts", "Host alerts (A3 pager)")
    try:
        rows = p.sqlite_query(p.paths.state_db,
                              "SELECT component, episode_open, episode_opened_at "
                              "FROM host_alert_status")
        out = p.sqlite_query(p.paths.state_db,
                             "SELECT count(*) AS n, min(created_at) AS oldest, "
                             "(SELECT max(delivered_at) FROM host_alert_outbox) AS last "
                             "FROM host_alert_outbox WHERE delivered_at IS NULL")
    except Exception as exc:  # noqa: BLE001
        return r(AMBER, f"can't read the alert DB ({type(exc).__name__})")
    o = out[0] if out else {"n": 0, "oldest": None, "last": None}
    oldest = _epoch(o.get("oldest"))
    last_sent = _epoch(o.get("last"))
    open_eps = [x for x in rows if x.get("episode_open")]
    details = {"open_episodes": [x["component"] for x in open_eps],
               "undelivered": o.get("n", 0),
               "last_delivered": last_sent.isoformat() if last_sent else None}
    if o.get("n") and oldest and p.now() - oldest > timedelta(minutes=15):
        return r(RED, f"{o['n']} alert(s) stuck in the outbox since {_local(p, oldest)}",
                 since=oldest, hint="Telegram send path from the host is failing",
                 details=details)
    if open_eps:
        opened = [_epoch(x.get("episode_opened_at")) for x in open_eps]
        opened = [x for x in opened if x]
        return r(AMBER, "open episode(s): " + ", ".join(x["component"] for x in open_eps),
                 since=min(opened) if opened else None, details=details)
    return r(GREEN, f"no open episodes; outbox empty (last alert sent {_ago(p, last_sent)}). "
                    "Note: it pages on liveness, not function", details=details)


_OK_RESULTS = (0, None, 0x41301, 0x41303)  # ok, unknown, running, not yet run


def check_scheduled_tasks(p: Probes) -> CheckResult:
    r = _result("scheduled_tasks", "Narada scheduled tasks")
    tasks = p.scheduled_tasks()
    if not tasks:
        return r(AMBER, "couldn't read Task Scheduler")
    covered = {"smriti-nightly", "Narada_Host", "Narada_Deadman", "smriti-logd",
               "smriti-morning"}
    bad, disabled = [], []
    for name, t in sorted(tasks.items()):
        if name in covered:
            continue
        if t.state.lower() == "disabled":
            disabled.append(name)
        elif t.last_result not in _OK_RESULTS:
            bad.append(f"{name} 0x{t.last_result:X}")
    notes = []
    runs = _cron_runs(p.tail_text(p.paths.cron_logs / "Narada_Daily_Debrief.log",
                                  64 * 1024) or "")
    lens = []
    for run in runs[-3:]:
        m = next((re.search(r"\(len=(\d+)\)", ln) for ln in run["out"] if "len=" in ln), None)
        lens.append(int(m.group(1)) if m else None)
    if len(lens) == 3 and None not in lens and len(set(lens)) == 1 and lens[0] < 400:
        notes.append(f"Narada_Daily_Debrief exits 0 but sent the same {lens[0]}-char text "
                     "3 nights running (likely its failure notice: a false green)")
    details = {"failing": bad, "disabled": disabled, "notes": notes,
               "tasks": sorted(tasks)}
    problems = [f"last result {', '.join(bad)}"] if bad else []
    if disabled:
        problems.append("disabled: " + ", ".join(disabled))
    problems += notes
    if problems:
        return r(AMBER, "; ".join(problems), details=details)
    return r(GREEN, f"{len(tasks)} tasks, all last results ok", details=details)


def check_log_hygiene(p: Probes) -> CheckResult:
    r = _result("log_hygiene", "Secrets in host.log")
    tail = p.tail_text(p.paths.host_log, 512 * 1024) or ""
    if re.search(r"api\.telegram\.org/bot\d{6,}:", tail):
        return r(AMBER, "host.log records the Telegram bot token (the bridge's httpx "
                        "request logging at INFO)",
                 hint="set the httpx logger to WARNING in narada_chat_bridge.py (the "
                      "telegram_bridge check then reads amber, not red); rotate the token "
                      "if logs were shared")
    return r(GREEN, "no bot token in the recent host.log")


# ── registry ──────────────────────────────────────────────────────────

CHECKS: list[tuple[str, Callable[[Probes], CheckResult]]] = [
    ("host", check_host),
    ("components", check_components),
    ("brain_local", check_brain_local),
    ("brain_tailnet", check_brain_tailnet),
    ("brain_sessions", check_brain_sessions),
    ("telegram_bridge", check_telegram_bridge),
    ("telegram_inbound", check_telegram_inbound),
    ("livekit", check_livekit),
    ("box_dispatch", check_box_dispatch),
    ("voice_box", check_voice_box),
    ("voice_phone", check_voice_phone),
    ("box_device", check_box_device),
    ("push_subs", check_push_subs),
    ("smriti_nightly", check_smriti_nightly),
    ("smriti_tasks", check_smriti_tasks),
    ("morning", check_morning),
    ("akhada_tick", check_akhada_tick),
    ("memory_backup", check_memory_backup),
    ("deadman", check_deadman),
    ("host_alerts", check_host_alerts),
    ("scheduled_tasks", check_scheduled_tasks),
    ("log_hygiene", check_log_hygiene),
]

TITLES = {
    "host": "Supervisor (Narada_Host)", "components": "Supervised components",
    "brain_local": "Brain :8811", "brain_tailnet": "Brain over tailnet :8443",
    "brain_sessions": "Brain sessions",
    "telegram_bridge": "Telegram bridge polling",
    "telegram_inbound": "Telegram inbound → reply", "livekit": "LiveKit media server",
    "box_dispatch": "BOX voice: agent dispatch", "voice_box": "BOX voice worker",
    "voice_phone": "Phone voice worker", "box_device": "BOX device (ESP32-S3-BOX-3)",
    "push_subs": "Phone push subscriptions", "smriti_nightly": "smriti nightly",
    "smriti_tasks": "smriti logd", "morning": "Morning message (the daily heartbeat)",
    "akhada_tick": "akhada check-in tick", "memory_backup": "Memory backup pushed (GitHub)",
    "deadman": "Dead-man watchdog (deadman.ps1)", "host_alerts": "Host alerts (A3 pager)",
    "scheduled_tasks": "Narada scheduled tasks", "log_hygiene": "Secrets in host.log",
}

__all__ = ["CHECKS", "TITLES", "worst"]
