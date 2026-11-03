"""The doctor's only window onto the machine. Every check reads through
a Probes object, so tests swap in a fake and the real one stays the
single place where I/O happens.

Hard rules for everything in here (Suti, 2026-11-03):
- read-only: no restarts, no config writes, no messages, no fetches
  that move refs; sqlite opens with mode=ro; git runs with optional
  locks off;
- bounded: every network call and subprocess has a timeout;
- secret-blind: secrets used for an API call (LiveKit) stay in-process
  and are never returned, logged or put in an exception message.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from pathlib import Path


# ── paths ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Paths:
    home: Path
    local: Path  # %LOCALAPPDATA%\narada — the host's state dir

    @classmethod
    def default(cls) -> "Paths":
        home = Path.home()
        local = os.environ.get("LOCALAPPDATA")
        return cls(home=home,
                   local=Path(local) / "narada" if local else home / ".narada")

    @property
    def narada(self) -> Path:
        return self.home / ".narada"

    # host
    @property
    def components_yaml(self) -> Path:
        return self.narada / "host" / "components.yaml"

    @property
    def livekit_yaml(self) -> Path:
        return self.narada / "host" / "livekit.yaml"

    @property
    def doctor_json(self) -> Path:
        return self.narada / "host" / "doctor.json"

    @property
    def heartbeat(self) -> Path:
        return self.local / "host.heartbeat"

    @property
    def host_lock(self) -> Path:
        return self.local / "host.lock"

    @property
    def host_log(self) -> Path:
        return self.local / "logs" / "host.log"

    @property
    def deadman_log(self) -> Path:
        return self.local / "logs" / "deadman.log"

    @property
    def deadman_state(self) -> Path:
        return self.local / "deadman-state.json"

    @property
    def cron_logs(self) -> Path:
        return self.local / "logs" / "cron"

    @property
    def voice_fault(self) -> Path:
        return self.local / "voice-fault.json"

    @property
    def state_db(self) -> Path:
        return self.narada / "state.db"

    # voice
    @property
    def wake_model(self) -> Path:
        return Path(os.environ.get(
            "NARADA_WAKE_MODEL",
            str(self.narada / "wakeword" / "output" / "narada" / "narada.onnx")))

    @property
    def voice_budget(self) -> Path:
        return self.narada / "voice-budget.json"

    # brain / telegram
    @property
    def push_subs(self) -> Path:
        return self.narada / "brain" / "push-subs.json"

    @property
    def brain_sessions(self) -> Path:
        return self.narada / "brain" / "sessions"

    @property
    def chat_cycles(self) -> Path:
        return self.narada / "heartbeat" / "chat-cycles"

    @property
    def chat_sessions(self) -> Path:
        return self.narada / "chat-sessions"

    @property
    def claude_projects(self) -> Path:
        return self.home / ".claude" / "projects"

    # smriti
    @property
    def nightly_status(self) -> Path:
        return self.narada / "log" / ".nightly-status.json"

    @property
    def morning_status(self) -> Path:
        return self.narada / "log" / ".morning-status.json"

    @property
    def logd_pid(self) -> Path:
        return self.narada / "log" / ".logd.pid"

    @property
    def logd_state(self) -> Path:
        return self.narada / "log" / ".state.json"

    @property
    def smriti_metrics(self) -> Path:
        return self.narada / ".smriti" / "metrics.jsonl"

    @property
    def qmd_index(self) -> Path:
        return Path(os.environ.get(
            "QMD_INDEX", str(self.home / ".cache" / "qmd" / "index.sqlite")))


# ── result records ────────────────────────────────────────────────────

@dataclass
class HttpResult:
    status: int | None  # None = no HTTP answer (refused, timeout, DNS)
    body: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300


@dataclass
class RunResult:
    rc: int | None  # None = could not run / timed out
    out: str = ""
    err: str = ""


@dataclass
class TaskInfo:
    name: str
    state: str
    last_run: datetime | None
    last_result: int | None
    next_run: datetime | None


# ── the real probes ───────────────────────────────────────────────────

_PS_TASKS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$rows = Get-ScheduledTask | Where-Object { $_.TaskName -match '^(Narada_|smriti-)' } | ForEach-Object {
  $i = $_ | Get-ScheduledTaskInfo
  [pscustomobject]@{
    name   = $_.TaskName
    state  = [string]$_.State
    last   = if ($i.LastRunTime -and $i.LastRunTime.Year -gt 2000) { $i.LastRunTime.ToUniversalTime().ToString('o') } else { $null }
    result = $i.LastTaskResult
    next   = if ($i.NextRunTime) { $i.NextRunTime.ToUniversalTime().ToString('o') } else { $null }
  }
}
@($rows) | ConvertTo-Json -Compress -Depth 3
"""


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Probes:
    """Real, read-only, bounded I/O."""

    def __init__(self, paths: Paths | None = None,
                 tailnet_base: str | None = None,
                 local_tz: tzinfo | None = None) -> None:
        self.paths = paths or Paths.default()
        self.tailnet_base = tailnet_base if tailnet_base is not None else os.environ.get(
            "NARADA_DOCTOR_TAILNET", "https://<machine>.<tailnet>.ts.net")
        self.local_tz = local_tz or datetime.now().astimezone().tzinfo
        self._lock = threading.Lock()
        self._tasks: dict[str, TaskInfo] | None = None

    # time
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    # files
    def exists(self, path: Path) -> bool:
        try:
            return path.exists()
        except OSError:
            return False

    def read_text(self, path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            return None

    def tail_text(self, path: Path, max_bytes: int) -> str | None:
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - max_bytes))
                data = fh.read()
        except OSError:
            return None
        text = data.decode("utf-8", errors="replace")
        if len(data) == max_bytes:  # drop the partial first line
            text = text.split("\n", 1)[-1]
        return text

    def mtime(self, path: Path) -> datetime | None:
        try:
            return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        except OSError:
            return None

    def listdir(self, path: Path) -> list[str]:
        try:
            return sorted(os.listdir(path))
        except OSError:
            return []

    def glob(self, root: Path, pattern: str) -> list[Path]:
        try:
            return sorted(root.glob(pattern))
        except OSError:
            return []

    # network
    def http_get(self, url: str, timeout: float = 5.0) -> HttpResult:
        req = urllib.request.Request(url, method="GET",
                                     headers={"User-Agent": "narada-doctor"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read(65536).decode("utf-8", errors="replace")
                return HttpResult(resp.status, body)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(4096).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                body = ""
            return HttpResult(exc.code, body)
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            return HttpResult(None, "", f"{type(reason).__name__}: {reason}"[:200])

    # processes
    def run(self, argv: list[str], timeout: float = 20.0,
            env: dict | None = None) -> RunResult:
        try:
            proc = subprocess.run(
                argv, capture_output=True, timeout=timeout,
                env={**os.environ, **(env or {})},
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            return RunResult(None, "", f"timed out after {timeout:.0f}s")
        except OSError as exc:
            return RunResult(None, "", f"{type(exc).__name__}: {exc}")
        return RunResult(proc.returncode,
                         proc.stdout.decode("utf-8", errors="replace"),
                         proc.stderr.decode("utf-8", errors="replace"))

    def git(self, repo: Path, *args: str, timeout: float = 15.0) -> RunResult:
        return self.run(["git", "-C", str(repo), *args], timeout=timeout,
                        env={"GIT_OPTIONAL_LOCKS": "0",
                             "GIT_TERMINAL_PROMPT": "0"})

    def pid_alive(self, pid: int) -> bool:
        try:
            import psutil
            return psutil.pid_exists(int(pid))
        except Exception:  # noqa: BLE001
            return False

    def scheduled_tasks(self) -> dict[str, TaskInfo]:
        """Narada_* and smriti-* tasks, one PowerShell call per run."""
        with self._lock:
            if self._tasks is not None:
                return self._tasks
            res = self.run(["powershell.exe", "-NoProfile", "-NonInteractive",
                            "-Command", _PS_TASKS], timeout=12)
            tasks: dict[str, TaskInfo] = {}
            if res.rc == 0 and res.out.strip():
                try:
                    rows = json.loads(res.out)
                except ValueError:
                    rows = []
                if isinstance(rows, dict):
                    rows = [rows]
                for r in rows:
                    tasks[r["name"]] = TaskInfo(
                        name=r["name"], state=r.get("state") or "",
                        last_run=_parse_iso(r.get("last")),
                        last_result=r.get("result"),
                        next_run=_parse_iso(r.get("next")))
            self._tasks = tasks
            return tasks

    # databases
    def sqlite_query(self, path: Path, sql: str, params: tuple = ()) -> list[dict]:
        uri = "file:" + str(path).replace(os.sep, "/") + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=3)
        try:
            con.row_factory = sqlite3.Row
            return [dict(r) for r in con.execute(sql, params).fetchall()]
        finally:
            con.close()

    # LiveKit (read-only RoomService calls; the secret never leaves here)
    def livekit_participants(self, room: str) -> list[dict] | None:
        """Participants of `room`; None when the room doesn't exist.
        Raises RuntimeError (without secret material) on failure."""
        try:
            import yaml
            cfg = yaml.safe_load(self.paths.livekit_yaml.read_text(encoding="utf-8"))
            key, secret = next(iter((cfg.get("keys") or {}).items()))
            port = int(cfg.get("port", 7880))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"livekit config unreadable ({type(exc).__name__})") from None

        def b64(raw: bytes) -> str:
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        def token(grants: dict) -> str:
            now = int(time.time())
            head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
            body = b64(json.dumps({"iss": key, "sub": "narada-doctor",
                                   "nbf": now - 5, "exp": now + 60,
                                   "video": grants}).encode())
            sig = b64(hmac.new(str(secret).encode(), f"{head}.{body}".encode(),
                               hashlib.sha256).digest())
            return f"{head}.{body}.{sig}"

        def call(method: str, payload: dict, grants: dict) -> dict:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/twirp/livekit.RoomService/{method}",
                data=json.dumps(payload).encode(), method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + token(grants)})
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"livekit {method} failed ({type(exc).__name__})") from None

        rooms = call("ListRooms", {"names": [room]}, {"roomList": True}).get("rooms") or []
        if not any(r.get("name") == room for r in rooms):
            return None
        parts = call("ListParticipants", {"room": room},
                     {"roomAdmin": True, "room": room}).get("participants") or []
        return [{"identity": p.get("identity", ""),
                 "kind": p.get("kind", "STANDARD"),
                 "state": p.get("state", ""),
                 "joined_at": p.get("joinedAt")} for p in parts]
