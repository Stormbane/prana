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


def _vapid_pem_path() -> Path:
    """pywebpush wants a PATH (or raw base64), never PEM content — a
    PEM string is misread as base64 DER ("header too long", found live
    2026-09-07). Materialize the key to a file and hand paths around."""
    pem = _root() / "vapid-private.pem"
    data = ensure_vapid()
    try:
        if not pem.exists() or pem.read_text(encoding="utf-8") != data["private_pem"]:
            pem.write_text(data["private_pem"], encoding="utf-8")
    except OSError:
        logger.warning("could not materialize vapid pem", exc_info=True)
    return pem


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


def _log_notification(title: str, body: str) -> None:
    """Append to the notification feed the app's pinned Notifications
    chat reads — every notify-suti message lands here even when no
    device is push-subscribed yet."""
    import time

    path = _root() / "notifications.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                "title": title, "body": body}) + "\n")
        # Cap the feed: rewrite when it grows past ~400 entries.
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > 400:
            path.write_text("\n".join(lines[-200:]) + "\n", encoding="utf-8")
    except OSError:
        logger.warning("could not log notification", exc_info=True)


def read_notifications(limit: int = 50) -> list[dict]:
    path = _root() / "notifications.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def send(title: str, body: str, *, url: str = "./",
         webpush_fn=None) -> dict:
    """Push {title, body, url} to every subscription. Best-effort per
    device; expired ones are pruned. Returns {sent, failed, dropped}."""
    _log_notification(title, body)
    if webpush_fn is None:
        from pywebpush import webpush as webpush_fn  # type: ignore[no-redef]
    from pywebpush import WebPushException

    vapid_key_path = str(_vapid_pem_path())
    payload = json.dumps({"title": title, "body": body, "url": url})
    with _LOCK:
        subs = _load_subs()
    sent, failed, dropped = 0, 0, []
    for endpoint, sub in list(subs.items()):
        try:
            webpush_fn(
                subscription_info=sub,
                data=payload,
                vapid_private_key=vapid_key_path,
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
