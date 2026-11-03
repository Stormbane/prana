"""The doctor's runner, output shapes, digest design and the real
probes' safety properties (read-only, bounded, secret-blind)."""
from __future__ import annotations

import json
import socket
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

from doctor_fakes import NOW, TZ, FakeProbes, healthy_world
from prana.doctor import __main__ as cli
from prana.doctor.digest import compose_digest, digest_due, fingerprint
from prana.doctor.model import AMBER, CARD_KIND, GREEN, RED, SCHEMA, CheckResult, Report, redact
from prana.doctor.probes import Paths, Probes
from prana.doctor.runner import render_text, run_checks, write_json


@pytest.fixture(autouse=True)
def _no_env_overrides(monkeypatch):
    monkeypatch.delenv("NARADA_WAKE_MODEL", raising=False)
    monkeypatch.delenv("QMD_INDEX", raising=False)


def _report(*statuses: str) -> Report:
    checks = [CheckResult(id=f"c{i}", title=f"Check {i}", status=s, reason=f"reason {i}",
                          since=NOW - timedelta(hours=i), hint="do the thing" if s != GREEN else "")
              for i, s in enumerate(statuses)]
    return Report(generated_at=NOW, checks=checks, host="pc")


# ── runner ────────────────────────────────────────────────────────────

def test_crashing_check_is_amber_and_redacted():
    def boom(_p):
        raise ValueError("bad token=abcdefghijklmnopqrstuv in config")
    report = run_checks(FakeProbes(), checks=[("boom", boom)], timeout_s=5)
    (res,) = report.checks
    assert res.status == AMBER and res.reason.startswith("check crashed: ValueError")
    assert "abcdefghijklmnopqrstuv" not in res.reason


def test_hung_check_times_out_as_amber_without_blocking():
    def hang(_p):
        time.sleep(2.5)
    started = time.monotonic()
    report = run_checks(FakeProbes(), checks=[("hang", hang)], timeout_s=0.5)
    assert time.monotonic() - started < 2
    (res,) = report.checks
    assert res.status == AMBER and "timed out" in res.reason


def test_check_returning_nothing_is_amber():
    report = run_checks(FakeProbes(), checks=[("none", lambda _p: None)], timeout_s=5)
    (res,) = report.checks
    assert res.status == AMBER and "not a CheckResult" in res.reason


def test_only_filter_runs_named_checks():
    report = run_checks(healthy_world(), timeout_s=10, only={"push_subs", "deadman"})
    assert {c.id for c in report.checks} == {"push_subs", "deadman"}


def test_overall_and_exit_codes():
    assert _report(GREEN, GREEN).exit_code() == 0
    assert _report(GREEN, AMBER).exit_code() == 1
    assert _report(AMBER, RED, GREEN).overall == RED
    assert _report(AMBER, RED, GREEN).exit_code() == 2


def test_render_text_worst_first_with_hints_and_since():
    text = render_text(_report(GREEN, RED, AMBER), FakeProbes())
    body = text.splitlines()
    assert "overall: RED" in body[0] and "(1 red, 1 amber, 1 green)" in body[0]
    assert body[2].startswith("RED") and body[3].strip().startswith("-> do the thing")
    assert "[since 2026-11-03 22:40]" in text  # local time


# ── doctor.json + card ────────────────────────────────────────────────

def test_write_json_is_atomic_and_complete(tmp_path):
    path = tmp_path / "host" / "doctor.json"
    write_json(_report(GREEN, RED), path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema"] == SCHEMA and data["overall"] == RED
    assert data["counts"] == {"green": 1, "amber": 0, "red": 1}
    assert data["checks"][1]["since"] == "2026-11-03T12:40:00Z"
    assert data["card"]["kind"] == CARD_KIND
    assert [p.name for p in path.parent.iterdir()] == ["doctor.json"]  # no temp left


def test_card_shape_orders_red_first_and_is_self_contained():
    card = _report(GREEN, AMBER, RED).to_card()
    assert card["version"] == 1 and card["stale_after_s"] > 0
    assert [i["status"] for i in card["items"]] == [RED, AMBER, GREEN]
    assert card["headline"] == "1 needs attention: Check 2"
    assert set(card["items"][0]) == {"id", "label", "status", "reason", "since",
                                     "since_approx", "action"}
    assert card["items"][2]["action"] is None


def test_card_headlines():
    assert _report(GREEN).to_card()["headline"] == "All systems well"
    assert _report(GREEN, AMBER).to_card()["headline"] == "Working, 1 to watch"


def test_report_output_is_redacted():
    leak = "bot123456789:AAH" + "y" * 32
    r = Report(generated_at=NOW, checks=[CheckResult("x", "X", RED, f"saw {leak}",
                                                     details={"line": leak})])
    blob = json.dumps(r.to_dict())
    assert leak not in blob and "bot<redacted>" in blob


@pytest.mark.parametrize("secret", [
    "Bearer abcdefghijklmnop", "sk-proj-abcdefghijklmnopqrst",
    "api_key=ABCDEFGHIJKLMNOP1234", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4eHh4In0.c2lnbmF0dXJlMTIz",
])
def test_redact_patterns(secret):
    assert secret not in redact(f"x {secret} y")


# ── digest (design only) ──────────────────────────────────────────────

def test_digest_none_when_nothing_red():
    assert compose_digest(_report(GREEN, AMBER), TZ) is None


def test_digest_names_failing_step_and_next_action():
    fp = healthy_world()
    fp.put_json(fp.paths.nightly_status, {
        "started": "2026-11-03T03:00:02+10:00", "target_day": "2026-11-02", "ok": False,
        "steps": {"index": {"ok": False, "error": "No embedding provider available"}}})
    report = run_checks(fp, timeout_s=10)
    text = compose_digest(report, TZ)
    assert text.startswith("Narada doctor, Tue 03 Nov: 1 red.")
    assert "smriti nightly: last night (2026-11-02) failed at step index" in text
    assert "next:" in text and len(text) <= 3500


def test_digest_due_once_per_day_only_when_red():
    red, green = _report(RED), _report(GREEN)
    today = date(2026, 11, 3)
    assert digest_due(red, None, today)
    assert not digest_due(red, {"day": "2026-11-03", "fingerprint": fingerprint(red)}, today)
    assert digest_due(red, {"day": "2026-11-02"}, today)
    assert not digest_due(green, None, today)


def test_fingerprint_tracks_red_reasons_only():
    a, b = _report(RED, GREEN), _report(RED, AMBER)
    assert fingerprint(a) == fingerprint(b)
    c = _report(RED)
    c.checks[0].reason = "different"
    assert fingerprint(c) != fingerprint(a)


# ── CLI ───────────────────────────────────────────────────────────────

def test_cli_writes_only_with_flag(tmp_path, monkeypatch, capsys):
    fp = healthy_world(FakeProbes(home=tmp_path / "home"))
    monkeypatch.setattr(cli, "Probes", lambda: fp)
    assert cli.main([]) == 0
    assert not fp.paths.doctor_json.exists()
    assert "overall: GREEN" in capsys.readouterr().out
    assert cli.main(["--write"]) == 0
    assert json.loads(fp.paths.doctor_json.read_text(encoding="utf-8"))["overall"] == GREEN


def test_cli_card_and_digest_modes(monkeypatch, capsys):
    fp = healthy_world()
    fp.put(fp.paths.push_subs, "{}")
    monkeypatch.setattr(cli, "Probes", lambda: fp)
    assert cli.main(["--card", "--only", "push_subs"]) == 2
    card = json.loads(capsys.readouterr().out)
    assert card["overall"] == RED and card["items"][0]["id"] == "push_subs"
    cli.main(["--digest", "--only", "push_subs"])
    assert "Phone push subscriptions: no devices subscribed" in capsys.readouterr().out


def test_cli_rejects_unknown_check_ids(monkeypatch, capsys):
    """A typo must not run zero checks and report all green."""
    monkeypatch.setattr(cli, "Probes", lambda: healthy_world())
    with pytest.raises(SystemExit) as exc:
        cli.main(["--only", "push_sbus"])
    assert exc.value.code == 2
    assert "unknown check id(s): push_sbus" in capsys.readouterr().err


# ── the digest sender: the one place the doctor speaks ────────────────

import subprocess  # noqa: E402


def _red_report():
    fp = healthy_world()
    fp.put(fp.paths.push_subs, "{}")
    return run_checks(fp, timeout_s=10, only={"push_subs"}), fp


def test_send_digest_sends_once_a_day_through_notify_suti(tmp_path):
    from datetime import date
    from prana.doctor import send
    report, fp = _red_report()
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, "", "delivered via: telegram, app-push")

    state = tmp_path / "doctor-digest.json"
    rc, note = send.send_digest(report, TZ, state, tmp_path / "narada", date(2026, 11, 3),
                                run=fake_run)
    assert rc == 0 and note == "digest sent"
    argv, kw = calls[0]
    assert argv[-1].endswith("notify_suti.py")
    assert kw["env"]["NOTIFY_LEVEL"] == "error" and kw["env"]["NOTIFY_SOURCE"] == "doctor"
    assert kw["env"]["PYTHONUTF8"] == "1"  # the child decodes the digest's "→" correctly
    assert "Phone push subscriptions" in kw["input"]
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["day"] == "2026-11-03" and saved["streak"] == 1
    # a second run the same day sends nothing
    rc, note = send.send_digest(report, TZ, state, tmp_path / "narada", date(2026, 11, 3),
                                run=fake_run)
    assert rc == 0 and len(calls) == 1
    # the next day, the same reds are marked as a repeat
    send.send_digest(report, TZ, state, tmp_path / "narada", date(2026, 11, 4), run=fake_run)
    assert calls[1][1]["input"].startswith("(Unchanged, day 2.)")


def test_send_digest_failure_leaves_state_for_a_retry(tmp_path):
    from datetime import date
    from prana.doctor import send
    report, _ = _red_report()
    state = tmp_path / "doctor-digest.json"
    rc, note = send.send_digest(report, TZ, state, tmp_path, date(2026, 11, 3),
                                run=lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "delivered via: NOTHING"))
    assert rc == 1 and "exited 1" in note and not state.exists()


def test_cli_send_digest_exit_code_reports_delivery_not_health(monkeypatch):
    """Red is the digest's job; the scheduled task must read 0 when it
    delivered, and 3 only when delivery failed."""
    from prana.doctor import send
    fp = healthy_world()
    fp.put(fp.paths.push_subs, "{}")
    monkeypatch.setattr(cli, "Probes", lambda: fp)
    monkeypatch.setattr(send, "send_digest", lambda *a, **k: (0, "digest sent"))
    assert cli.main(["--send-digest"]) == 0
    monkeypatch.setattr(send, "send_digest", lambda *a, **k: (1, "notify_suti exited 1"))
    assert cli.main(["--send-digest"]) == 3


def test_send_digest_nothing_red_sends_nothing(tmp_path):
    from datetime import date
    from prana.doctor import send
    report = run_checks(healthy_world(), timeout_s=10)
    rc, note = send.send_digest(report, TZ, tmp_path / "s.json", tmp_path, date(2026, 11, 3),
                                run=lambda *a, **k: pytest.fail("must not send"))
    assert rc == 0


# ── the real probes: read-only, bounded, secret-blind ─────────────────

def test_real_sqlite_probe_is_read_only(tmp_path):
    db = tmp_path / "x.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (a)")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    con.close()
    p = Probes(Paths(home=tmp_path, local=tmp_path), tailnet_base="")
    assert p.sqlite_query(db, "SELECT a FROM t") == [{"a": 1}]
    with pytest.raises(sqlite3.OperationalError):
        p.sqlite_query(db, "INSERT INTO t VALUES (2)")


def test_real_tail_drops_partial_first_line(tmp_path):
    f = tmp_path / "log"
    f.write_bytes(b"first line\nsecond line\nthird\n")
    p = Probes(Paths(home=tmp_path, local=tmp_path), tailnet_base="")
    assert p.tail_text(f, 12) == "third\n"
    assert p.tail_text(tmp_path / "missing", 10) is None


def test_real_http_probe_is_bounded_on_a_dead_port(tmp_path):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here now
    p = Probes(Paths(home=tmp_path, local=tmp_path), tailnet_base="")
    started = time.monotonic()
    res = p.http_get(f"http://127.0.0.1:{port}/", timeout=2)
    assert res.status is None and not res.ok and time.monotonic() - started < 5


def test_real_livekit_probe_never_leaks_the_secret(tmp_path, monkeypatch):
    secret = "S3cretS3cretS3cretS3cretS3cretS3cret"
    host = tmp_path / ".narada" / "host"
    host.mkdir(parents=True)
    (host / "livekit.yaml").write_text(f"port: 1\nkeys:\n  narada-key: {secret}\n",
                                       encoding="utf-8")
    p = Probes(Paths(home=tmp_path, local=tmp_path), tailnet_base="")
    with pytest.raises(RuntimeError) as exc:
        p.livekit_participants("narada-body")  # port 1: refused
    assert secret not in str(exc.value) and exc.value.__cause__ is None


def test_real_livekit_probe_missing_config(tmp_path):
    p = Probes(Paths(home=tmp_path, local=tmp_path), tailnet_base="")
    with pytest.raises(RuntimeError, match="config unreadable"):
        p.livekit_participants("narada-body")


def test_paths_default_uses_localappdata(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    paths = Paths.default()
    assert paths.local == tmp_path / "narada"
    assert paths.doctor_json == Path.home() / ".narada" / "host" / "doctor.json"
