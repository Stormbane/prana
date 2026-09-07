"""Web Push for the phone app — the app leg of notify-suti fan-out.

The brain owns the push machinery because it is same-origin with the
installed PWA: the app subscribes through its bearer-authed API, the
VAPID keypair and subscription list live under ``~/.narada/brain/``,
and the notify-suti dispatcher (or anything holding a prana-tier
token) POSTs one message that fans out to every subscribed device.

Expired subscriptions (404/410 from the push service) are dropped on
send — a phone that uninstalled the PWA silently stops counting.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger("brain.push")

_LOCK = threading.Lock()


def _root() -> Path:
    return Path.home() / ".narada" / "brain"


def _vapid_path() -> Path:
    return _root() / "vapid.json"


def _subs_path() -> Path:
    return _root() / "push-subs.json"


def ensure_vapid() -> dict:
    """Load or create the VAPID keypair. Returns {private_pem, public_key}
    where public_key is the urlsafe-b64 uncompressed P-256 point the
    browser's pushManager.subscribe wants."""
    path = _vapid_path()
    with _LOCK:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("private_pem") and data.get("public_key"):
                return data
        except (OSError, ValueError):
            pass
        from py_vapid import Vapid, b64urlencode
        from cryptography.hazmat.primitives import serialization

        vapid = Vapid()
        vapid.generate_keys()
        private_pem = vapid.private_pem().decode("ascii")
        raw = vapid.public_key.public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint)
        data = {"private_pem": private_pem, "public_key": b64urlencode(raw)}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return data


def _load_subs() -> dict[str, dict]:
    try:
        return json.loads(_subs_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_subs(subs: dict[str, dict]) -> None:
    path = _subs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(subs, indent=2), encoding="utf-8")


def subscribe(subscription: dict) -> int:
    """Store one browser subscription (keyed by endpoint). Returns count."""
    endpoint = subscription.get("endpoint", "")
    if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
        raise ValueError("subscription needs an https endpoint")
    if not isinstance(subscription.get("keys"), dict):
        raise ValueError("subscription needs keys")
    with _LOCK:
        subs = _load_subs()
        subs[endpoint] = subscription
        _save_subs(subs)
        return len(subs)


def unsubscribe(endpoint: str) -> int:
    with _LOCK:
        subs = _load_subs()
        subs.pop(endpoint, None)
        _save_subs(subs)
        return len(subs)


def send(title: str, body: str, *, url: str = "./",
         webpush_fn=None) -> dict:
    """Push {title, body, url} to every subscription. Best-effort per
    device; expired ones are pruned. Returns {sent, failed, dropped}."""
    if webpush_fn is None:
        from pywebpush import webpush as webpush_fn  # type: ignore[no-redef]
    from pywebpush import WebPushException

    vapid = ensure_vapid()
    payload = json.dumps({"title": title, "body": body, "url": url})
    with _LOCK:
        subs = _load_subs()
    sent, failed, dropped = 0, 0, []
    for endpoint, sub in list(subs.items()):
        try:
            webpush_fn(
                subscription_info=sub,
                data=payload,
                vapid_private_key=vapid["private_pem"],
                vapid_claims={"sub": "mailto:7186653+Stormbane@users.noreply.github.com"},
            )
            sent += 1
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                dropped.append(endpoint)
            else:
                failed += 1
                logger.warning("push to %s… failed: %s", endpoint[:40], exc)
        except Exception as exc:  # noqa: BLE001 — one bad device must not stop the rest
            failed += 1
            logger.warning("push to %s… failed: %s", endpoint[:40], exc)
    if dropped:
        with _LOCK:
            subs = _load_subs()
            for endpoint in dropped:
                subs.pop(endpoint, None)
            _save_subs(subs)
    return {"sent": sent, "failed": failed, "dropped": len(dropped)}
