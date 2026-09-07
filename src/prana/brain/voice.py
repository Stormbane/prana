"""Phone-app voice plumbing: LiveKit join tokens and agent dispatch.

Adapted from the proven akhada dashboard implementation
(akhada ``dashboard/voice.py``, branch rung1-phone-voice → master):
stdlib-only HS256 token mint + explicit twirp dispatch. The brain
serves the narada phone app the same two-step flow the /talk page
proved out: GET a session (room + join token), join, THEN dispatch
the phone worker into the occupied room — the agent never lands in an
empty room.

The bearer tier on the endpoint IS the admission: only an
authenticated app/prana caller can mint a join token, and the token
is scoped to one fresh ``narada-phone-*`` room. The dispatch endpoint
refuses any other room prefix, so it can never poke the box's rooms
(and the phone worker's explicit agent_name keeps it out of automatic
dispatch entirely). Credentials come from ``~/.narada/.livekit.env``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.request
from pathlib import Path

ROOM_PREFIX = "narada-phone-"
PHONE_IDENTITY = "suti-phone"
# Same env override the worker itself honors — one knob, no drift.
AGENT_NAME = os.environ.get("AKHADA_AGENT_NAME", "akhada-phone")
TOKEN_TTL_S = 15 * 60  # join within 15 min; the session cap rules after


def load_livekit_env(path: Path | None = None) -> dict[str, str]:
    """Parse ~/.narada/.livekit.env (KEY=VALUE lines, # comments)."""
    path = path or Path.home() / ".narada" / ".livekit.env"
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def mint_token(api_key: str, api_secret: str, identity: str,
               grants: dict, ttl_s: int = TOKEN_TTL_S,
               now: int | None = None) -> str:
    """A LiveKit HS256 access token (JWT), no library needed."""
    now = int(time.time()) if now is None else now
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": api_key,
        "sub": identity,
        "iat": now,
        "nbf": now - 10,
        "exp": now + ttl_s,
        "video": grants,
    }
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    )
    sig = hmac.new(api_secret.encode(), signing_input.encode(),
                   hashlib.sha256).digest()
    return signing_input + "." + _b64url(sig)


def new_room_name() -> str:
    return f"{ROOM_PREFIX}{int(time.time() * 1000)}-{secrets.token_hex(3)}"


def phone_session(env: dict[str, str] | None = None) -> dict:
    """Everything the app needs to join: room, token, identity, agent.

    ``url`` is empty on purpose: the client derives wss://<host>:7443
    from its own https origin (tailscale serve maps 7443 → LiveKit),
    which is correct for the tailnet without the server knowing its
    own public name. Override via NARADA_PHONE_LIVEKIT_WS_URL if that
    ever stops being true.
    """
    env = env if env is not None else load_livekit_env()
    key, secret = env.get("LIVEKIT_API_KEY"), env.get("LIVEKIT_API_SECRET")
    if not key or not secret:
        raise RuntimeError("LIVEKIT_API_KEY/SECRET missing from "
                           "~/.narada/.livekit.env")
    room = new_room_name()
    token = mint_token(key, secret, PHONE_IDENTITY, {
        "room": room,
        "roomJoin": True,
        "canPublish": True,
        "canSubscribe": True,
        "canPublishData": True,
    })
    return {"room": room, "token": token, "identity": PHONE_IDENTITY,
            "url": os.environ.get("NARADA_PHONE_LIVEKIT_WS_URL", ""),
            "agent": AGENT_NAME}


def _http_base(livekit_url: str) -> str:
    if livekit_url.startswith("wss://"):
        return "https://" + livekit_url[len("wss://"):]
    if livekit_url.startswith("ws://"):
        return "http://" + livekit_url[len("ws://"):]
    return livekit_url


def dispatch_agent(room: str, env: dict[str, str] | None = None,
                   agent_name: str | None = None,
                   opener=urllib.request.urlopen) -> dict:
    """Explicitly dispatch the phone worker into `room` (twirp API).

    Called by the app AFTER it has joined, so the agent never lands in
    an empty room (the soak's ordering lesson). Refuses non-phone rooms.
    """
    if not room.startswith(ROOM_PREFIX):
        raise ValueError(f"refusing dispatch outside {ROOM_PREFIX}*: {room!r}")
    env = env if env is not None else load_livekit_env()
    key, secret = env.get("LIVEKIT_API_KEY"), env.get("LIVEKIT_API_SECRET")
    url = env.get("LIVEKIT_URL", "ws://127.0.0.1:7880")
    if not key or not secret:
        raise RuntimeError("LIVEKIT_API_KEY/SECRET missing")
    admin = mint_token(key, secret, "narada-brain",
                       {"roomAdmin": True, "room": room}, ttl_s=60)
    body = json.dumps({"agent_name": agent_name or AGENT_NAME,
                       "room": room}).encode()
    req = urllib.request.Request(
        _http_base(url) + "/twirp/livekit.AgentDispatchService/CreateDispatch",
        data=body, method="POST",
        headers={"Authorization": f"Bearer {admin}",
                 "Content-Type": "application/json"})
    with opener(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")
