"""Every doctor check against a fake machine: the healthy world is all
green, and each failure mode found on 2026-11-03 turns its check the
right colour with a reason that names the cause."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from doctor_fakes import (NOW, TZ, FakeProbes, cron_block, healthy_world, local_line,
                          poll_line, task)
from prana.doctor import checks as C
from prana.doctor.model import AMBER, GREEN, RED
from prana.doctor.probes import HttpResult, RunResult
from prana.doctor.runner import run_checks


@pytest.fixture(autouse=True)
def _no_env_overrides(monkeypatch):
    monkeypatch.delenv("NARADA_WAKE_MODEL", raising=False)
    monkeypatch.delenv("QMD_INDEX", raising=False)


@pytest.fixture
def fp() -> FakeProbes:
    return healthy_world()


def test_healthy_world_is_all_green(fp):
    report = run_checks(fp, timeout_s=10)
    bad = {c.id: (c.status, c.reason) for c in report.checks if c.status != GREEN}
    assert bad == {}
    assert {c.id for c in report.checks} == {cid for cid, _ in C.CHECKS}
    assert report.exit_code() == 0


def test_checks_never_spawn_processes_or_send(fp):
    """FakeProbes.run raises: no check may shell out except through the
    read-only git/scheduled_tasks probes, and none POSTs anywhere."""
    run_checks(fp, timeout_s=10)
    assert all(c.startswith(("GET ", "git ")) for c in fp.calls)
    git_verbs = {c.split()[1] for c in fp.calls if c.startswith("git ")}
    assert git_verbs <= {"rev-parse", "rev-list", "log"}  # no fetch/push/commit


# ── supervisor ────────────────────────────────────────────────────────

def test_host_stale_heartbeat_is_red(fp):
    fp.put_json(fp.paths.heartbeat, {"ts": NOW.timestamp() - 900, "pid": 100})
    res = C.check_host(fp)
    assert res.status == RED and "stale" in res.reason


def test_host_dead_pid_is_red(fp):
    fp.alive.discard(100)
    res = C.check_host(fp)
    assert res.status == RED and "not running" in res.reason


def test_host_late_heartbeat_is_amber(fp):
    fp.put_json(fp.paths.heartbeat, {"ts": NOW.timestamp() - 150, "pid": 100})
    assert C.check_host(fp).status == AMBER


def test_host_missing_beacon_is_red(fp):
    del fp.files[fp.paths.heartbeat]
    assert C.check_host(fp).status == RED


def test_host_since_is_lock_start(fp):
    res = C.check_host(fp)
    assert res.since.isoformat().startswith("2026-10-22T05:31:15")


def test_components_down_is_red_and_names_it(fp):
    fp.http["http://127.0.0.1:7880/"] = HttpResult(None, "", "ConnectionRefusedError")
    res = C.check_components(fp)
    assert res.status == RED and "livekit" in res.reason
    assert "agent-gateway" not in res.reason  # disabled components aren't probed


def test_components_reports_unprobed(fp):
    res = C.check_components(fp)
    assert res.status == GREEN and "chat-bridge" in res.reason


# ── brain ─────────────────────────────────────────────────────────────

def test_brain_local_500_is_red(fp):
    fp.http[C.BRAIN_HEALTH] = HttpResult(500, "Internal Server Error")
    assert C.check_brain_local(fp).status == RED


def test_brain_tailnet_unreachable_is_red(fp):
    del fp.http["https://tail.example:8443/health"]
    assert C.check_brain_tailnet(fp).status == RED


def test_brain_tailnet_unconfigured_is_amber(fp):
    fp.tailnet_base = ""
    assert C.check_brain_tailnet(fp).status == AMBER


def test_brain_sessions_pruned_transcript_is_amber(fp):
    del fp.files[fp.paths.claude_projects / "C--sessions-telegram-42" / "abc-123.jsonl"]
    res = C.check_brain_sessions(fp)
    assert res.status == AMBER and "prana/telegram-42" in res.reason


def test_brain_sessions_ignores_codex_backend(fp):
    sess = fp.paths.brain_sessions / "app" / "phone-1"
    fp.put(sess / "native-session-id", "codex-thread")
    fp.put(sess / "backend", "codex")
    assert C.check_brain_sessions(fp).status == GREEN


# ── telegram ──────────────────────────────────────────────────────────

def test_bridge_no_polls_is_red(fp):
    fp.put(fp.paths.host_log, "")
    assert C.check_telegram_bridge(fp).status == RED


def test_bridge_late_poll_is_amber(fp):
    fp.put(fp.paths.host_log, poll_line(NOW - timedelta(minutes=5)) + "\n")
    assert C.check_telegram_bridge(fp).status == AMBER


def test_bridge_conflict_is_red(fp):
    fp.put(fp.paths.host_log, poll_line(NOW) + "\n" + local_line(
        NOW, "WARNING prana.host.[chat-bridge]: err: telegram.error.Conflict: terminated by "
             "other getUpdates request") + "\n")
    res = C.check_telegram_bridge(fp)
    assert res.status == RED and "another process" in res.reason


def test_inbound_failed_and_pruned_resume_is_red(fp):
    """The 2026-10-23 state: last two exchanges 500'd, and the session
    resumes a transcript Claude Code pruned after 30 days."""
    ok = fp.files.pop(fp.paths.chat_cycles / "2026-11-02-010000-chat-42.md")
    fp.put(fp.paths.chat_cycles / "2026-09-17-082340-chat-42.md", ok)
    for name in ("2026-10-22-160232-chat-42.md", "2026-10-23-021707-chat-42.md"):
        fp.put(fp.paths.chat_cycles / name,
               "# Chat exchange — FAILED\n\n## Suti\n\nx\n\n## Narada\n\n"
               "(brain 500: Internal Server Error)\n")
    del fp.files[fp.paths.claude_projects / "C--sessions-telegram-42" / "abc-123.jsonl"]
    res = C.check_telegram_inbound(fp)
    assert res.status == RED
    assert "FAILED (brain 500" in res.reason and "pruned transcript" in res.reason
    assert "/new" in res.hint
    # the streak starts at the first failure after the last OK
    assert res.since.isoformat().startswith("2026-10-22T16:02:32")
    # Suti's own words never appear in the reason
    assert "## Suti" not in res.reason


def test_inbound_ok_but_pruned_resume_is_red(fp):
    del fp.files[fp.paths.claude_projects / "C--sessions-telegram-42" / "abc-123.jsonl"]
    res = C.check_telegram_inbound(fp)
    assert res.status == RED and "next message will 500" in res.reason


def test_inbound_after_new_uses_fresh_generation(fp):
    """/new bumps the generation; the -g1 session has nothing to resume."""
    del fp.files[fp.paths.claude_projects / "C--sessions-telegram-42" / "abc-123.jsonl"]
    fp.put(fp.paths.chat_sessions / "42" / ".brain-generation", "1")
    res = C.check_telegram_inbound(fp)
    assert res.status == GREEN and res.details["brain_session"] == "telegram-42-g1"


def test_inbound_none_recorded_is_amber():
    assert C.check_telegram_inbound(FakeProbes()).status == AMBER


# ── voice / LiveKit / BOX ─────────────────────────────────────────────

def test_livekit_local_down_is_red(fp):
    del fp.http[C.LIVEKIT_LOCAL]
    assert C.check_livekit(fp).status == RED


def test_livekit_tailnet_down_is_amber(fp):
    del fp.http["https://tail.example:7443/"]
    res = C.check_livekit(fp)
    assert res.status == AMBER and "phone can't join" in res.reason


def _orphan_log(n: int = 4) -> str:
    lines = []
    for i in range(n):
        t = NOW - timedelta(seconds=44 * (n - i))
        lines.append(local_line(t, "WARNING prana.host.[voice]: err: WARNING:narada-voice:"
                                   "room narada-body orphaned (device present, no agent) "
                                   "— recycling for re-dispatch"))
        lines.append(local_line(t + timedelta(seconds=2),
                                "WARNING prana.host.[voice]: err: ERROR:narada-voice:wake model "
                                "unavailable (set NARADA_WAKE_GATING=off to bypass): wake "
                                "model missing"))
        status = "JS_FAILED" if i % 2 else "JS_SUCCESS"  # a bail ends either way
        lines.append(local_line(t + timedelta(seconds=3),
                                'WARNING prana.host.[livekit]: err: INFO livekit.agents '
                                f'job ended {{"workerID": "AW_1", "agentName": "", '
                                f'"status": "{status}", "error": ""}}'))
    return "\n".join(lines) + "\n"


def test_box_dispatch_wake_model_bail_is_red(fp):
    """The 2026-11-03 BOX failure: device in room, no agent, every job
    bails on the missing wake model, room recycled every ~44 s."""
    fp.participants = [{"identity": "narada-box3", "kind": "STANDARD", "state": "ACTIVE"}]
    fp.put(fp.paths.host_log, _orphan_log(4))
    del fp.files[fp.paths.wake_model]
    wakeword_dir = fp.paths.wake_model.parents[2]  # survives; output/ was moved away
    fp.dirs.add(wakeword_dir)
    fp.mtimes[wakeword_dir] = NOW - timedelta(days=12)
    res = C.check_box_dispatch(fp)
    assert res.status == RED
    assert "4 of 4 recent jobs bailed 'wake model unavailable'" in res.reason
    assert "every ~44s" in res.reason and "black dog" in res.reason
    assert res.since == NOW - timedelta(days=12) and res.since_approx


def test_box_dispatch_bail_loop_is_red_even_mid_recycle(fp):
    """Caught while the sweeper has deleted the room: still red."""
    fp.put(fp.paths.host_log, _orphan_log(3))
    fp.participants = None
    assert C.check_box_dispatch(fp).status == RED
    fp.participants = RuntimeError("livekit ListRooms failed (URLError)")
    assert C.check_box_dispatch(fp).status == RED


def test_box_dispatch_old_bails_with_room_absent_is_amber(fp):
    fp.put(fp.paths.host_log, _orphan_log(3))
    fp.clock = NOW + timedelta(minutes=30)
    fp.participants = None
    assert C.check_box_dispatch(fp).status == AMBER


def test_box_dispatch_agent_present_is_green(fp):
    assert C.check_box_dispatch(fp).status == GREEN


def test_box_dispatch_livekit_error_is_amber(fp):
    fp.participants = RuntimeError("livekit ListRooms failed (URLError)")
    assert C.check_box_dispatch(fp).status == AMBER


def test_box_dispatch_room_absent_is_amber(fp):
    fp.participants = None
    assert C.check_box_dispatch(fp).status == AMBER


def test_box_dispatch_orphan_without_wake_errors_is_red(fp):
    fp.participants = [{"identity": "narada-box3", "kind": "STANDARD", "state": "ACTIVE"}]
    log = "\n".join(ln for ln in _orphan_log(3).splitlines() if "wake model" not in ln)
    fp.put(fp.paths.host_log, log + "\n")
    res = C.check_box_dispatch(fp)
    assert res.status == RED and "room recycles" in res.reason


def test_voice_box_missing_model_points_at_archive(fp):
    del fp.files[fp.paths.wake_model]
    archive = C.WAKE_ARCHIVE_CANDIDATES[0]
    fp.put(archive, "onnx")
    res = C.check_voice_box(fp)
    assert res.status == RED and "wake model missing" in res.reason
    assert str(archive) in res.hint


def test_voice_box_credits_fault_is_red(fp):
    fp.put_json(fp.paths.voice_fault, {"code": "credits", "text": "OpenAI credit is empty.",
                                       "at": "2026-10-13T17:37:04"})
    res = C.check_voice_box(fp)
    assert res.status == RED and "OpenAI credit empty" in res.reason
    assert res.since.astimezone(TZ).strftime("%m-%d %H:%M") == "10-13 17:37"


def test_voice_box_health_down_is_red(fp):
    del fp.http[C.BOX_HEALTH]
    assert C.check_voice_box(fp).status == RED


def test_voice_box_other_fault_is_amber(fp):
    fp.put_json(fp.paths.voice_fault, {"code": "connect", "at": "2026-11-03T20:00:00"})
    assert C.check_voice_box(fp).status == AMBER


def test_voice_phone_credits_is_red(fp):
    fp.put_json(fp.paths.voice_fault, {"code": "credits", "at": "2026-10-13T17:37:04"})
    res = C.check_voice_phone(fp)
    assert res.status == RED and "shared voice backend" in res.reason


def test_voice_phone_down_is_red(fp):
    del fp.http[C.PHONE_HEALTH]
    assert C.check_voice_phone(fp).status == RED


def test_box_device_absent_is_red(fp):
    fp.participants = [{"identity": "agent-AJ_1", "kind": "AGENT", "state": "ACTIVE"}]
    assert C.check_box_device(fp).status == RED


def test_box_device_between_recycles_is_amber(fp):
    fp.participants = None  # room deleted by the orphan sweeper a moment ago
    fp.put(fp.paths.host_log, local_line(
        NOW - timedelta(seconds=40),
        'WARNING prana.host.[livekit]: err: INFO livekit rtc/room.go:1283 participant active '
        '{"room": "narada-body", "participant": "narada-box3", "remote": false}') + "\n")
    res = C.check_box_device(fp)
    assert res.status == AMBER and "between room recycles" in res.reason


def test_box_device_flapping_is_amber(fp):
    fp.on_sql("FROM host_alert_events", [{"kind": "health-fail", "at": NOW.timestamp() - i}
                                         for i in (300, 200, 100)])
    res = C.check_box_device(fp)
    assert res.status == AMBER and "dropped 3×" in res.reason


# ── push ──────────────────────────────────────────────────────────────

def test_push_subs_empty_is_red(fp):
    fp.put(fp.paths.push_subs, "{}", mtime=NOW - timedelta(days=56))
    res = C.check_push_subs(fp)
    assert res.status == RED and res.details["devices"] == 0
    assert res.since == NOW - timedelta(days=56)


def test_push_subs_missing_is_red(fp):
    del fp.files[fp.paths.push_subs]
    assert C.check_push_subs(fp).status == RED


# ── smriti ────────────────────────────────────────────────────────────

def test_nightly_failed_step_is_named(fp):
    fp.put_json(fp.paths.nightly_status, {
        "started": "2026-11-03T03:00:02+10:00", "target_day": "2026-11-02", "ok": False,
        "steps": {"reconcile": {"ok": True},
                  "index": {"ok": False, "error": "No embedding provider available (tried "
                                                  "local; last error: numpy.core.multiarray "
                                                  "failed to import). Install onnxruntime"},
                  "witness": {"ok": True}}})
    fp.put(fp.paths.deadman_log,
           "2026-10-24T17:10:00Z job-watch: smriti-nightly ok\n"
           "2026-10-25T17:06:00Z job-watch: task 'smriti-nightly' last run FAILED (result 1)\n"
           "2026-10-25T17:11:00Z job-watch: task 'smriti-nightly' last run FAILED (result 1) "
           "(already paged, standing down)\n")
    res = C.check_smriti_nightly(fp)
    assert res.status == RED
    assert "failed at step index" in res.reason and "numpy" in res.reason
    assert res.since.isoformat().startswith("2026-10-25T17:06")
    assert res.details["failed_steps"] == ["index"]


def test_nightly_not_run_last_night_is_red(fp):
    fp.put_json(fp.paths.nightly_status, {"started": "2026-11-01T03:00:00+10:00", "ok": True,
                                          "steps": {}})
    res = C.check_smriti_nightly(fp)
    assert res.status == RED and "hasn't run" in res.reason


def test_nightly_before_next_run_accepts_last_night(fp):
    # 02:50 local on 11-04: tonight's 03:00 run isn't due yet, so the
    # 11-03 03:00 run is still "last night".
    fp.clock = NOW.replace(hour=16, minute=50)
    assert C.check_smriti_nightly(fp).status == GREEN


def test_nightly_overdue_after_run_window_is_red(fp):
    fp.clock = NOW + timedelta(hours=5)  # 04:40 local on 11-04, run should have happened
    assert C.check_smriti_nightly(fp).status == RED


def test_logd_dead_is_red(fp):
    fp.alive.discard(200)
    res = C.check_smriti_tasks(fp)
    assert res.status == RED and "logd" in res.reason


def test_morning_not_sent_is_red(fp):
    fp.put_json(fp.paths.morning_status, {"day": "2026-11-02", "sent": True})
    res = C.check_smriti_tasks(fp)
    assert res.status == RED and "morning message not sent for 2026-11-03" in res.reason


def test_qmd_stale_index_is_red(fp):
    fp.on_sql("FROM documents", [{"newest": "2026-09-06T14:42:44.887Z", "n": 6433}])
    res = C.check_qmd_index(fp)
    assert res.status == RED and "57 days old" in res.reason
    assert "rc 0" in res.reason  # the refresh says ok while nothing lands


def test_qmd_missing_is_red(fp):
    del fp.files[fp.paths.qmd_index]
    assert C.check_qmd_index(fp).status == RED


def test_qmd_unreadable_is_amber(fp):
    import sqlite3
    fp.on_sql("FROM documents", sqlite3.OperationalError("database is locked"))
    assert C.check_qmd_index(fp).status == AMBER


# ── akhada ────────────────────────────────────────────────────────────

def test_akhada_tick_failed_is_red(fp):
    fp.put(fp.paths.cron_logs / "Narada_Akhada_Checkin.log",
           cron_block(NOW - timedelta(hours=1), 1, "Traceback: boom"))
    res = C.check_akhada_tick(fp)
    assert res.status == RED and "rc=1" in res.reason


def test_akhada_tick_stale_is_red(fp):
    fp.put(fp.paths.cron_logs / "Narada_Akhada_Checkin.log",
           cron_block(NOW - timedelta(hours=20), 0, "nudge sent via telegram:42"))
    assert C.check_akhada_tick(fp).status == RED


def test_akhada_tick_no_runs_is_red(fp):
    del fp.files[fp.paths.cron_logs / "Narada_Akhada_Checkin.log"]
    assert C.check_akhada_tick(fp).status == RED


# ── memory backup ─────────────────────────────────────────────────────

def _unpushed(fp, n: int, oldest_age: timedelta) -> None:
    fp.git_runs[("rev-list", "--count", "@{u}..HEAD")] = RunResult(0, f"{n}\n")
    stamps = [(NOW - oldest_age + timedelta(minutes=i)).astimezone(TZ).isoformat()
              for i in range(n)]
    fp.git_runs[("log", "@{u}..HEAD", "--format=%cI")] = RunResult(0, "\n".join(reversed(stamps)))


def test_backup_78_unpushed_is_red(fp):
    _unpushed(fp, 78, timedelta(days=8))
    res = C.check_memory_backup(fp)
    assert res.status == RED and res.reason.startswith("78 commit(s) unpushed")
    assert res.since == NOW - timedelta(days=8)


def test_backup_recent_unpushed_is_amber(fp):
    _unpushed(fp, 3, timedelta(hours=20))
    assert C.check_memory_backup(fp).status == AMBER


def test_backup_fresh_unpushed_is_green(fp):
    _unpushed(fp, 1, timedelta(minutes=20))
    assert C.check_memory_backup(fp).status == GREEN


def test_backup_no_upstream_is_red(fp):
    fp.git_runs[("rev-parse", "--abbrev-ref", "@{u}")] = RunResult(128, "", "fatal: no upstream")
    assert C.check_memory_backup(fp).status == RED


# ── watchdogs ─────────────────────────────────────────────────────────

def test_deadman_silent_is_red(fp):
    fp.put(fp.paths.deadman_log, "2026-11-03T13:00:00Z healthy (age 12s)\n")
    assert C.check_deadman(fp).status == RED


def test_deadman_exhausted_is_red(fp):
    fp.put_json(fp.paths.deadman_state, {"restarts": [NOW.timestamp() - 600] * 3,
                                         "paged_exhausted": True})
    res = C.check_deadman(fp)
    assert res.status == RED and "exhausted" in res.reason


def test_deadman_cannot_page_is_amber(fp):
    del fp.files[fp.paths.home / ".hermes" / ".env"]
    res = C.check_deadman(fp)
    assert res.status == AMBER and "can't page" in res.reason


def test_deadman_recent_restart_is_amber(fp):
    fp.put_json(fp.paths.deadman_state, {"restarts": [NOW.timestamp() - 600],
                                         "paged_exhausted": False})
    assert C.check_deadman(fp).status == AMBER


def test_host_alerts_stuck_outbox_is_red(fp):
    fp.on_sql("host_alert_outbox", [{"n": 2, "oldest": NOW.timestamp() - 3600, "last": None}])
    res = C.check_host_alerts(fp)
    assert res.status == RED and "stuck" in res.reason


def test_host_alerts_open_episode_is_amber(fp):
    fp.on_sql("FROM host_alert_status", [{"component": "voice", "episode_open": 1,
                                          "episode_opened_at": NOW.timestamp() - 60,
                                          "healthy_since": None}])
    res = C.check_host_alerts(fp)
    assert res.status == AMBER and "voice" in res.reason


def test_host_alerts_db_error_is_amber(fp):
    fp.on_sql("FROM host_alert_status", RuntimeError("locked"))
    assert C.check_host_alerts(fp).status == AMBER


def test_scheduled_task_failing_is_amber(fp):
    fp.tasks["Narada_Scene_Nightly"] = task("Narada_Scene_Nightly", result=1)
    res = C.check_scheduled_tasks(fp)
    assert res.status == AMBER and "Narada_Scene_Nightly 0x1" in res.reason


def test_scheduled_tasks_ignores_covered_and_running(fp):
    fp.tasks["Narada_Host"] = task("Narada_Host", result=1)  # covered by check_host
    fp.tasks["Narada_Figures"] = task("Narada_Figures", result=0x41301)  # running
    assert C.check_scheduled_tasks(fp).status == GREEN


def test_debrief_false_green_is_amber(fp):
    fp.put(fp.paths.cron_logs / "Narada_Daily_Debrief.log", "".join(
        cron_block(NOW - timedelta(days=d), 0, "debrief delivered_to=telegram:42 (len=130)")
        for d in (3, 2, 1)))
    res = C.check_scheduled_tasks(fp)
    assert res.status == AMBER and "false green" in res.reason


def test_scheduler_unreadable_is_amber(fp):
    fp.tasks = {}
    assert C.check_scheduled_tasks(fp).status == AMBER


def test_log_hygiene_flags_token_without_printing_it(fp):
    token = "123456789:AAH" + "x" * 32
    fp.put(fp.paths.host_log, local_line(
        NOW, f'... POST https://api.telegram.org/bot{token}/getUpdates "HTTP/1.1 200 OK"'))
    res = C.check_log_hygiene(fp)
    assert res.status == AMBER
    assert token not in json.dumps(res.to_dict())
