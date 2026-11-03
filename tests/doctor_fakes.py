"""In-memory fake of prana.doctor.probes.Probes, plus a builder for a
fully healthy world. Tests flip one fact and assert one verdict."""
from __future__ import annotations

import fnmatch
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from prana.doctor.checks import BOX_HEALTH, BRAIN_HEALTH, LIVEKIT_LOCAL, PHONE_HEALTH
from prana.doctor.probes import HttpResult, Paths, RunResult, TaskInfo

TZ = timezone(timedelta(hours=10))  # Brisbane, no DST
NOW = datetime(2026, 11, 3, 13, 40, tzinfo=timezone.utc)  # 23:40 local
TAILNET = "https://tail.example"


class FakeProbes:
    def __init__(self, home: Path | None = None) -> None:
        home = home or Path("H:/home")
        self.paths = Paths(home=home, local=home / "AppData" / "narada")
        self.tailnet_base = TAILNET
        self.local_tz = TZ
        self.clock = NOW
        self.files: dict[Path, str] = {}
        self.mtimes: dict[Path, datetime] = {}
        self.dirs: set[Path] = set()
        self.http: dict[str, HttpResult] = {}
        self.git_runs: dict[tuple, RunResult] = {}
        self.tasks: dict[str, TaskInfo] = {}
        self.alive: set[int] = set()
        self.sql: list[tuple[str, object]] = []
        self.participants: object = None
        self.calls: list[str] = []

    # helpers for tests
    def put(self, path: Path, text: str, mtime: datetime | None = None) -> None:
        self.files[path] = text
        if mtime:
            self.mtimes[path] = mtime

    def put_json(self, path: Path, obj, mtime: datetime | None = None) -> None:
        self.put(path, json.dumps(obj), mtime)

    def on_sql(self, fragment: str, rows) -> None:
        self.sql.insert(0, (fragment, rows))  # newest registration wins

    def _all_dirs(self) -> set[Path]:
        out = set(self.dirs)
        for f in list(self.files) + list(self.dirs):
            out.update(f.parents)
        return out

    # the Probes surface
    def now(self) -> datetime:
        return self.clock

    def exists(self, path: Path) -> bool:
        return path in self.files or path in self._all_dirs()

    def read_text(self, path: Path) -> str | None:
        return self.files.get(path)

    def tail_text(self, path: Path, max_bytes: int) -> str | None:
        text = self.files.get(path)
        return None if text is None else text[-max_bytes:]

    def mtime(self, path: Path) -> datetime | None:
        if path in self.mtimes:
            return self.mtimes[path]
        return NOW - timedelta(minutes=1) if self.exists(path) else None

    def listdir(self, path: Path) -> list[str]:
        kids = {p.name for p in list(self.files) + list(self._all_dirs())
                if p.parent == path}
        return sorted(kids)

    def glob(self, root: Path, pattern: str) -> list[Path]:
        out = []
        for f in self.files:
            try:
                rel = f.relative_to(root).as_posix()
            except ValueError:
                continue
            if fnmatch.fnmatch(rel, pattern):
                out.append(f)
        return sorted(out)

    def http_get(self, url: str, timeout: float = 5.0) -> HttpResult:
        self.calls.append(f"GET {url}")
        return self.http.get(url, HttpResult(None, "", "ConnectionRefusedError: refused"))

    def run(self, argv, timeout: float = 20.0, env=None) -> RunResult:
        raise AssertionError(f"unexpected subprocess in a check: {argv}")

    def git(self, repo: Path, *args: str, timeout: float = 15.0) -> RunResult:
        self.calls.append("git " + " ".join(args))
        return self.git_runs.get(tuple(args), RunResult(128, "", "fatal: no fake"))

    def pid_alive(self, pid: int) -> bool:
        return int(pid) in self.alive

    def scheduled_tasks(self) -> dict[str, TaskInfo]:
        return self.tasks

    def sqlite_query(self, path: Path, sql: str, params: tuple = ()) -> list[dict]:
        for fragment, rows in self.sql:
            if fragment in sql:
                if isinstance(rows, Exception):
                    raise rows
                return rows(params) if callable(rows) else rows
        raise RuntimeError(f"no fake sql for: {sql[:60]}")

    def livekit_participants(self, room: str):
        if isinstance(self.participants, Exception):
            raise self.participants
        return self.participants


def local_line(dt: datetime, body: str) -> str:
    """A host.log line: local-time prefix, as prana.host.log writes it."""
    return f"{dt.astimezone(TZ).strftime('%Y-%m-%d %H:%M:%S')} {body}"


def poll_line(dt: datetime, status: str = "200 OK") -> str:
    return local_line(dt, "WARNING prana.host.[chat-bridge]: err: 23:39:50 [INFO] httpx: "
                          "HTTP Request: POST https://api.telegram.org/bot<redacted>/getUpdates "
                          f'"HTTP/1.1 {status}"')


def task(name: str, result: int = 0, last: datetime | None = None,
         state: str = "Ready") -> TaskInfo:
    return TaskInfo(name=name, state=state, last_run=last or NOW - timedelta(hours=1),
                    last_result=result, next_run=NOW + timedelta(hours=1))


def cron_block(start: datetime, rc: int, *out: str) -> str:
    stamp = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    return "\n".join([f"=== {stamp} run: C:\\Projects\\prana\\scripts\\x.py ===", *out,
                      f"=== {stamp} exit rc={rc} ===", ""])


def healthy_world(fp: FakeProbes | None = None) -> FakeProbes:
    fp = fp or FakeProbes()
    P = fp.paths
    epoch = NOW.timestamp()

    # supervisor
    fp.put_json(P.heartbeat, {"ts": epoch - 10, "pid": 100})
    fp.put_json(P.host_lock, {"pid": 100, "start_time": "2026-10-22T05:31:15+00:00"})
    fp.alive |= {100, 200}
    fp.put(P.components_yaml, """
components:
  - {name: chat-bridge, enabled: true, command: [python]}
  - {name: brain, enabled: true, command: [python], health_url: "http://127.0.0.1:8811/health"}
  - {name: livekit, enabled: true, command: [lk], health_url: "http://127.0.0.1:7880/"}
  - {name: agent-gateway, enabled: false, command: [x], health_url: "http://127.0.0.1:1/"}
""")
    fp.on_sql("FROM host_alert_status", [
        {"component": "brain", "healthy_since": epoch - 86400, "episode_open": 0,
         "episode_opened_at": None},
        {"component": "livekit", "healthy_since": epoch - 86400, "episode_open": 0,
         "episode_opened_at": None},
        {"component": "chat-bridge", "healthy_since": epoch - 5 * 86400, "episode_open": 0,
         "episode_opened_at": None},
    ])
    fp.on_sql("host_alert_outbox", [{"n": 0, "oldest": None, "last": epoch - 3 * 86400}])
    fp.on_sql("FROM host_alert_events", [])

    # http
    ok = HttpResult(200, "ok")
    fp.http.update({
        BRAIN_HEALTH: HttpResult(200, '{"ok": true, "sessions": 1, "busy": 0}'),
        LIVEKIT_LOCAL: ok, f"{TAILNET}:8443/health": ok, f"{TAILNET}:7443/": ok,
        BOX_HEALTH: ok, PHONE_HEALTH: ok,
    })

    # telegram + brain sessions
    fp.put(P.host_log, "\n".join([
        poll_line(NOW - timedelta(seconds=40)),
        local_line(NOW - timedelta(seconds=30),
                   'WARNING prana.host.[livekit]: err: INFO livekit.agents job ended '
                   '{"workerID": "AW_1", "agentName": "", "jobType": "JT_ROOM", '
                   '"status": "JS_SUCCESS", "error": ""}'),
        poll_line(NOW - timedelta(seconds=20)),
    ]) + "\n")
    fp.put(P.chat_cycles / "2026-11-02-010000-chat-42.md",
           "# Chat exchange — OK\n\nchat_id: 42\n\n## Suti\n\nhi\n\n## Narada\n\nhello\n")
    sess = P.brain_sessions / "prana" / "telegram-42"
    fp.put(sess / "native-session-id", "abc-123\n")
    fp.put(sess / "backend", "claude")
    fp.put(P.claude_projects / "C--sessions-telegram-42" / "abc-123.jsonl", "{}\n")

    # voice / BOX
    fp.participants = [{"identity": "narada-box3", "kind": "STANDARD", "state": "ACTIVE"},
                       {"identity": "agent-AJ_1", "kind": "AGENT", "state": "ACTIVE"}]
    fp.put(P.wake_model, "onnx")
    fp.put_json(P.voice_budget, {"days": {"2026-11-02": 3.5}, "reservations": {}})

    # push
    fp.put_json(P.push_subs, {"https://push.example/1": {"keys": {}}})

    # smriti
    fp.put_json(P.nightly_status, {
        "started": "2026-11-03T03:00:02+10:00", "target_day": "2026-11-02", "ok": True,
        "steps": {"reconcile": {"ok": True}, "index": {"ok": True},
                  "recall_refresh": {"ok": True}}})
    fp.put(P.logd_pid, "200")
    fp.put(P.logd_state, "{}", mtime=NOW - timedelta(minutes=5))
    fp.put_json(P.morning_status, {"day": "2026-11-03", "sent": True})
    fp.put(P.qmd_index, "")
    fp.on_sql("FROM documents", [{"newest": "2026-11-03T01:00:00Z", "n": 6400}])
    fp.put(P.smriti_metrics, json.dumps({"timestamp": "2026-11-02T17:05:18Z",
                                         "event": "recall_index_refresh", "stage": "embed",
                                         "returncode": 0}) + "\n")

    # akhada tick
    fp.put(P.cron_logs / "Narada_Akhada_Checkin.log",
           cron_block(NOW - timedelta(hours=1, minutes=40), 0,
                      "nudge sent via telegram:42 [night]: Day-close check-in",
                      "app: channels=['badge', 'push'] push={'sent': 1}"))

    # memory backup
    fp.git_runs.update({
        ("rev-parse", "--abbrev-ref", "@{u}"): RunResult(0, "origin/master\n"),
        ("rev-list", "--count", "@{u}..HEAD"): RunResult(0, "0\n"),
        ("log", "-g", "-1", "--date=iso-strict", "--format=%gd%x09%gs",
         "refs/remotes/origin/master"):
            RunResult(0, "refs/remotes/origin/master@{2026-11-03T22:56:16+10:00}\tupdate by push\n"),
    })

    # watchdogs
    fp.put(P.deadman_log, "2026-11-03T13:30:00Z healthy (age 12s)\n"
                          "2026-11-03T13:35:00Z healthy (age 19s)\n"
                          "2026-11-03T13:35:01Z job-watch: smriti-nightly ok\n")
    fp.put_json(P.deadman_state, {"restarts": [], "paged_exhausted": False})
    fp.put(P.home / ".hermes" / ".env", "x")

    # scheduled tasks
    for name in ("Narada_Host", "Narada_Deadman", "Narada_Figures", "Narada_Akhada_Checkin",
                 "Narada_Daily_Debrief", "smriti-logd", "smriti-morning", "smriti-nightly"):
        fp.tasks[name] = task(name)
    fp.put(P.cron_logs / "Narada_Daily_Debrief.log", "".join(
        cron_block(NOW - timedelta(days=d), 0, f"debrief delivered_to=telegram:42 (len={n})")
        for d, n in ((3, 1500), (2, 1720), (1, 1610))))
    return fp
