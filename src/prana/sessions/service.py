"""The persistent session service — the process that OWNS the sessions.

Why this exists (Codex P1, phase-1 review): MCP servers are spawned per
``claude -p`` invocation and die with it. A process that owns Job
Objects (KILL_ON_JOB_CLOSE) must outlive the chat turn that asked for
the spawn — otherwise a session started from chat is killed the moment
the response completes. So process ownership lives here, in a
long-lived service supervised by the host orchestrator, and every MCP
server is a thin client (:class:`ServiceClient`).

Transport: line-delimited JSON over TCP on 127.0.0.1 (CPython has no
AF_UNIX on Windows — this is the native-Windows IPC CCC lacked).
Requests: ``{"auth": <service token>, "method": str, "params": {}}``.
Replies:  ``{"ok": true, "result": ...}`` | ``{"ok": false, "error": str}``.

Run: ``python -m prana.sessions.service`` (host component ``sessions``).
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import socketserver
import sys
import threading
from typing import Any, Optional

from prana.sessions.manager import ManagerConfig, SessionManager
from prana.sessions.registry import Session
from prana.sessions.tokens import load_or_create_tokens

logger = logging.getLogger(__name__)

HOST = "127.0.0.1"
DEFAULT_PORT = 8791
MAX_LINE = 1 * 1024 * 1024

# Methods a client may invoke, mapped to SessionManager calls below.
METHODS = (
    "ping", "list_sessions", "get", "recent_output",
    "spawn", "relay", "cancel", "sweep", "reconcile",
)


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Session):
        return {
            "id": value.id, "provider": value.provider, "cwd": value.cwd,
            "title": value.title, "state": value.state.value,
            "pane_id": value.pane_id,
            "provider_session_id": value.provider_session_id,
            "last_activity_at": value.last_activity_at,
            "last_error": value.last_error, "exit_code": value.exit_code,
        }
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    return value


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server: "SessionService" = self.server  # type: ignore[assignment]
        try:
            line = self.rfile.readline(MAX_LINE)
            request = json.loads(line)
            if not secrets.compare_digest(
                str(request.get("auth", "")), server.token
            ):
                self._reply({"ok": False, "error": "bad auth"})
                return
            method = str(request.get("method", ""))
            params = request.get("params") or {}
            if method not in METHODS or not isinstance(params, dict):
                self._reply({"ok": False, "error": f"unknown method {method!r}"})
                return
            result = server.dispatch(method, params)
            self._reply({"ok": True, "result": _to_jsonable(result)})
        except Exception as exc:
            logger.warning("request failed: %s", exc)
            try:
                self._reply({"ok": False, "error": str(exc)})
            except OSError:
                pass

    def _reply(self, obj: dict) -> None:
        self.wfile.write((json.dumps(obj) + "\n").encode("utf-8"))


class SessionService(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        manager: Optional[SessionManager] = None,
        port: int = DEFAULT_PORT,
        token: Optional[str] = None,
    ) -> None:
        super().__init__((HOST, port), _Handler)
        self.manager = manager or SessionManager(ManagerConfig())
        self.token = token or load_or_create_tokens()["service"]

    @property
    def port(self) -> int:
        return self.server_address[1]

    def dispatch(self, method: str, params: dict) -> Any:
        mgr = self.manager
        if method == "ping":
            return "pong"
        if method == "list_sessions":
            return mgr.list_sessions(live_only=bool(params.get("live_only", True)))
        if method == "get":
            return mgr.get(params["session_id"])
        if method == "recent_output":
            return mgr.recent_output(
                params["session_id"], limit=int(params.get("limit", 50))
            )
        if method == "spawn":
            return mgr.spawn(
                params["provider"], params["cwd"], params["prompt"],
                title=params.get("title", ""),
                idempotency_key=params.get("idempotency_key") or None,
                resume_session_id=params.get("resume_session_id") or None,
            )
        if method == "relay":
            return mgr.relay(params["session_id"], params["text"])
        if method == "cancel":
            return mgr.cancel(params["session_id"])
        if method == "sweep":
            return mgr.sweep()
        if method == "reconcile":
            return mgr.reconcile()
        raise ValueError(f"unhandled method {method}")


class ServiceUnavailable(RuntimeError):
    pass


class ServiceClient:
    """One-request-per-connection client. Duck-types the manager surface
    that mcp.py needs; results are plain dicts (already serialized)."""

    def __init__(self, port: Optional[int] = None, token: Optional[str] = None):
        self._port = port or int(
            os.environ.get("PRANA_SESSIONS_PORT", DEFAULT_PORT)
        )
        self._token = token or load_or_create_tokens()["service"]

    def _request(self, method: str, **params: Any) -> Any:
        payload = json.dumps(
            {"auth": self._token, "method": method, "params": params}
        ).encode("utf-8") + b"\n"
        try:
            with socket.create_connection((HOST, self._port), timeout=10) as sock:
                sock.sendall(payload)
                buf = bytearray()
                while b"\n" not in buf:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    if len(buf) > MAX_LINE:
                        raise ServiceUnavailable("oversized service reply")
        except OSError as exc:
            raise ServiceUnavailable(
                f"session service not reachable on {HOST}:{self._port} "
                f"({exc}) — is the 'sessions' host component running?"
            ) from exc
        reply = json.loads(bytes(buf).split(b"\n", 1)[0])
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error", "service error"))
        return reply.get("result")

    # manager-shaped surface (dict results) ---------------------------

    def ping(self) -> bool:
        return self._request("ping") == "pong"

    def list_sessions(self, *, live_only: bool = True) -> list[dict]:
        return self._request("list_sessions", live_only=live_only)

    def get(self, session_id: str) -> dict:
        return self._request("get", session_id=session_id)

    def recent_output(self, session_id: str, limit: int = 50) -> list[str]:
        return self._request("recent_output", session_id=session_id, limit=limit)

    def spawn(self, provider: str, cwd: str, prompt: str, *, title: str = "",
              idempotency_key: Optional[str] = None,
              resume_session_id: Optional[str] = None) -> dict:
        return self._request(
            "spawn", provider=provider, cwd=cwd, prompt=prompt, title=title,
            idempotency_key=idempotency_key,
            resume_session_id=resume_session_id,
        )

    def relay(self, session_id: str, text: str) -> bool:
        return self._request("relay", session_id=session_id, text=text)

    def cancel(self, session_id: str) -> dict:
        return self._request("cancel", session_id=session_id)

    def sweep(self) -> list[dict]:
        return self._request("sweep")


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    port = int(os.environ.get("PRANA_SESSIONS_PORT", DEFAULT_PORT))
    service = SessionService(port=port)
    logger.info("session service listening on %s:%d", HOST, service.port)

    def _sweep_loop() -> None:
        try:
            service.manager.sweep()
            service.manager.reconcile()
        except Exception as exc:
            logger.warning("sweep failed: %s", exc)
        t = threading.Timer(60.0, _sweep_loop)
        t.daemon = True
        t.start()

    _sweep_loop()
    try:
        service.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
