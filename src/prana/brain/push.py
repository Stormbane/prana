"""Notifications — the feed, the routing table, and Web Push.

The brain owns notifications because it is same-origin with the
installed PWA. Every notification carries a LEVEL set by its producer
(plan §T7; the host/watchdog handoff of 2026-09-07):

    log        happened, no action            → feed only
    warn       degraded, self-healing          → feed + in-app badge
    error      wrong, needs attention          → feed + push + telegram
    emergency  can't self-recover / net is down → feed + push + telegram, louder

A ROUTING table maps level → channels; producers never choose
channels. The feed records EVERY level.

Episodes (review round 3): rows group by (source, category). A repeat
inside an open episode bumps the count and takes the newest body; a
HIGHER level arriving inside an episode upgrades the row, re-runs
routing at the new level and resets its seen-state; a `log` for the
group closes the episode (recovery) and lands as its own row; a quiet
window (6h) also closes it. Uncategorized entries never group.

Producer identity: every notification may carry an `event_id`;
ingestion is idempotent on it, so transport retries never re-page or
inflate counts. `delivered_channels` says which channels the producer
already used (the deadman Telegrams its own emergencies) and are
skipped here; `replay=True` marks bulk reconciliation as feed-only.

Expired push subscriptions (404/410) are dropped on send.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger("brain.push")

_LOCK = threading.Lock()

# "message" (Suti 2026-09-09): regular conversation from Narada — always
# shown in the app with an unread mark, pushed and Telegrammed like an
# error, but it is not a fault. Ranked between warn and error so the
# old "warn+" filters keep meaning "worth a look".
LEVELS: tuple[str, ...] = ("log", "warn", "message", "error", "emergency")
DEFAULT_LEVEL = "error"  # migration-safe: unlabelled callers keep paging

ROUTING: dict[str, tuple[str, ...]] = {
    "log": (),
    "warn": ("badge",),
    "message": ("badge", "push", "telegram"),
    "error": ("badge", "push", "telegram"),
    "emergency": ("badge", "push", "telegram", "escalate"),
}

EPISODE_QUIET_S = 6 * 3600
MAX_ROWS = 400
MAX_EVENT_IDS = 1000
DEFAULT_URL = "./#/?notifications=1"


def level_rank(level: str) -> int:
    return LEVELS.index(level) if level in LEVELS else LEVELS.index(DEFAULT_LEVEL)


def route(level: str) -> tuple[str, ...]:
    return ROUTING.get(level, ROUTING[DEFAULT_LEVEL])


def _root() -> Path:
    return Path.home() / ".narada" / "brain"


def _vapid_path() -> Path:
    return _root() / "vapid.json"


def _subs_path() -> Path:
    return _root() / "push-subs.json"


def _feed_path() -> Path:
    return _root() / "notifications.json"


def _legacy_feed_path() -> Path:
    return _root() / "notifications.jsonl"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _now_ts() -> float:
    return time.time()


# ── VAPID ─────────────────────────────────────────────────────────────


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


# ── subscriptions ─────────────────────────────────────────────────────


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


# ── the feed (episodes) ───────────────────────────────────────────────


def _load_feed() -> dict:
    """{rows: [...], event_ids: [...]} — migrating the old jsonl once."""
    path = _feed_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("rows"), list):
            data.setdefault("event_ids", [])
            # event → row map (recheck 1); older files carried only a list.
            events = data.get("events")
            if not isinstance(events, dict):
                data["events"] = {e: None for e in data["event_ids"]}
            return data
    except (OSError, ValueError):
        pass
    rows: list[dict] = []
    legacy = _legacy_feed_path()
    try:
        for line in legacy.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            rows.append({
                "id": f"legacy-{len(rows)}",
                "at": e.get("at", ""), "first_at": e.get("at", ""),
                "level": DEFAULT_LEVEL, "source": "legacy", "category": None,
                "title": e.get("title", "Narada"), "body": e.get("body", ""),
                "url": DEFAULT_URL, "count": 1, "open": False,
            })
    except OSError:
        pass
    return {"rows": rows, "event_ids": [], "events": {}}


def _save_feed(data: dict) -> None:
    path = _feed_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data["rows"] = data["rows"][-MAX_ROWS:]
    data["event_ids"] = data["event_ids"][-MAX_EVENT_IDS:]
    keep = set(data["event_ids"])
    data["events"] = {e: r for e, r in data.get("events", {}).items() if e in keep}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)
    try:
        _legacy_feed_path().unlink()
    except OSError:
        pass


def read_notifications(limit: int = 100) -> list[dict]:
    with _LOCK:
        return _load_feed()["rows"][-limit:]


def _parse_ts(iso: str) -> float:
    try:
        return time.mktime(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return 0.0


def _mark_delivered(row_id: str, outcomes: dict[str, object]) -> None:
    """Persist per-channel delivery outcomes on a row, so a later retry
    of the same event attempts only what failed (review P2)."""
    with _LOCK:
        data = _load_feed()
        for r in data["rows"]:
            if r.get("id") == row_id:
                delivered = dict(r.get("delivered", {}))
                delivered.update(outcomes)
                r["delivered"] = delivered
                break
        _save_feed(data)


def record(*, level: str, source: str, title: str, body: str,
           category: str | None = None, url: str = DEFAULT_URL,
           event_id: str | None = None) -> tuple[dict, bool, bool]:
    """Fold one notification into the feed.

    Returns (row, is_new_event, escalated): is_new_event False means
    the event_id was seen before (idempotent no-op); escalated True
    means an open episode's level went UP (routing must re-run)."""
    level = level if level in LEVELS else DEFAULT_LEVEL
    with _LOCK:
        data = _load_feed()
        if event_id and event_id in data["event_ids"]:
            row_id = data.get("events", {}).get(event_id)
            existing = next((r for r in reversed(data["rows"])
                             if r.get("id") == row_id
                             or (row_id is None and r.get("event_id") == event_id)),
                            None)
            return (existing or {}), False, False
        rows = data["rows"]
        now_iso, now_ts = _now_iso(), _now_ts()
        row: dict | None = None
        escalated = False
        # A conversation message is never an "episode" (review): two
        # messages six minutes apart are two things Narada said, not one
        # fault repeating — each gets its own row; an open fault episode
        # in the same category is left alone.
        if category and level != "message":
            open_row = next(
                (r for r in reversed(rows)
                 if r.get("open") and r.get("source") == source
                 and r.get("category") == category), None)
            if open_row is not None and now_ts - _parse_ts(open_row["at"]) > EPISODE_QUIET_S:
                open_row["open"] = False  # quiet window closed it
                open_row = None
            if open_row is not None:
                if level == "log":
                    open_row["open"] = False  # recovery closes the episode
                else:
                    open_row["count"] = int(open_row.get("count", 1)) + 1
                    open_row["at"] = now_iso
                    # A monotonic revision: `at` is second-resolution and
                    # two updates can share it (review). Read/alarm keys
                    # in the app are id@rev.
                    open_row["rev"] = int(open_row.get("rev", 1)) + 1
                    open_row["title"], open_row["body"] = title, body
                    if level_rank(level) > level_rank(open_row["level"]):
                        open_row["level"] = level
                        open_row["escalated_at"] = now_iso
                        escalated = True
                    if event_id:
                        open_row["event_id"] = event_id
                    # Latest activity last: the feed and the retention
                    # cap both read insertion order (review P2).
                    rows.remove(open_row)
                    rows.append(open_row)
                    row = open_row
        if row is None:
            row = {
                "id": f"n-{int(now_ts * 1000)}-{len(rows)}",
                "at": now_iso, "first_at": now_iso,
                "level": level, "source": source, "category": category,
                "title": title, "body": body, "url": url,
                "count": 1, "rev": 1,
                "open": bool(category) and level not in ("log", "message"),
                **({"event_id": event_id} if event_id else {}),
            }
            rows.append(row)
        if event_id:
            data["event_ids"].append(event_id)
            data.setdefault("events", {})[event_id] = row["id"]
        _save_feed(data)
        return dict(row), True, escalated


# ── delivery ──────────────────────────────────────────────────────────


def _push_all(title: str, body: str, url: str, level: str, webpush_fn=None,
              row: dict | None = None) -> dict:
    if webpush_fn is None:
        from pywebpush import webpush as webpush_fn  # type: ignore[no-redef]
    from pywebpush import WebPushException

    vapid_key_path = str(_vapid_pem_path())
    # id + at: the row's identity AND revision, so the app can dedupe the
    # alarm between the push door and the feed door (review).
    payload = json.dumps({"title": title, "body": body, "url": url, "level": level,
                          "id": (row or {}).get("id"), "at": (row or {}).get("at"),
                          "rev": (row or {}).get("rev", 1)})
    with _LOCK:
        subs = _load_subs()
    sent, failed, dropped = 0, 0, []
    for endpoint, sub in list(subs.items()):
        try:
            webpush_fn(
                subscription_info=sub, data=payload,
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


def _telegram(text: str, telegram_fn=None) -> bool:
    """Secondary sink for error+ — the proven notify_telegram.py hook,
    run out of process so a Telegram wobble never touches the brain."""
    if telegram_fn is not None:
        return bool(telegram_fn(text))
    hook = Path.home() / ".narada" / ".smriti" / "hooks" / "notify_telegram.py"
    if not hook.exists():
        return False
    try:
        proc = subprocess.run(
            [sys.executable, str(hook)], input=text.encode("utf-8"),
            capture_output=True, timeout=60,
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        return proc.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def notify(*, body: str, title: str = "Narada", level: str = DEFAULT_LEVEL,
           source: str = "brain", category: str | None = None,
           url: str = DEFAULT_URL, event_id: str | None = None,
           delivered_channels: tuple[str, ...] | list[str] = (),
           replay: bool = False, webpush_fn=None, telegram_fn=None) -> dict:
    """Record, then deliver per the routing table. The single door."""
    level = level if level in LEVELS else DEFAULT_LEVEL
    row, is_new, escalated = record(
        level=level, source=source, title=title, body=body,
        category=category, url=url, event_id=event_id)
    result: dict = {"row": row, "new": is_new, "escalated": escalated,
                    "channels": [], "push": None, "telegram": None}
    already = dict(row.get("delivered", {}))
    if not is_new:
        # A known event: no new row, no count bump — but any channel
        # that FAILED (False) or was left PENDING (None: the brain
        # stopped mid-delivery) is retried. Fully delivered or
        # deliberately skipped channels are no-ops.
        channels = [c for c in route(row.get("level", level))
                    if c in ("push", "telegram")
                    and already.get(c, "absent") in (False, None)
                    and c not in set(delivered_channels)]
    else:
        channels = [c for c in route(row.get("level", level))
                    if c not in set(delivered_channels)]
        # A repeat inside an open episode only pages again if it escalated.
        if int(row.get("count", 1)) > 1 and not escalated:
            channels = [c for c in channels if c == "badge"]
    if replay:
        # Bulk reconciliation is feed-only whichever branch we came by.
        channels = [c for c in channels if c == "badge"]
    result["channels"] = channels
    # Persist PENDING before touching any sink (recheck 3): an interrupted
    # attempt must look retryable, never delivered. Channels the producer
    # delivered, or replay suppressed, are recorded as skipped.
    pending: dict[str, object] = {c: None for c in channels if c in ("push", "telegram")}
    for c in ("push", "telegram"):
        if c in route(row.get("level", level)) and c not in channels and c not in already:
            pending[c] = "skipped"
    if pending and row.get("id"):
        _mark_delivered(row["id"], pending)
    outcomes: dict[str, object] = {}
    # Each channel fails ALONE (review P1): a push subsystem that cannot
    # even initialise must never take Telegram down with it.
    if "push" in channels:
        try:
            result["push"] = _push_all(title, body, url, row.get("level", level), webpush_fn,
                                       row=row)
            outcomes["push"] = int(result["push"].get("failed", 0)) == 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("push subsystem failed: %s", exc)
            result["push"] = {"sent": 0, "failed": 1, "dropped": 0, "error": str(exc)[:200]}
            outcomes["push"] = False
    if "telegram" in channels:
        try:
            result["telegram"] = _telegram(
                f"{title}: {body}" if title != "Narada" else body, telegram_fn)
        except Exception as exc:  # noqa: BLE001
            logger.warning("telegram sink failed: %s", exc)
            result["telegram"] = False
        outcomes["telegram"] = bool(result["telegram"])
    if outcomes and row.get("id"):
        _mark_delivered(row["id"], outcomes)
        row["delivered"] = {**already, **outcomes}
    return result


def send(title: str, body: str, *, url: str = DEFAULT_URL,
         webpush_fn=None) -> dict:
    """Back-compat door for /api/push/send: an unlabelled notification
    is an `error` (the migration default) from source "push-send";
    Telegram is left to the caller, which historically sent it."""
    r = notify(body=body, title=title, level=DEFAULT_LEVEL, source="push-send",
               url=url, delivered_channels=("telegram",), webpush_fn=webpush_fn)
    return r["push"] or {"sent": 0, "failed": 0, "dropped": 0}


def _log_notification(title: str, body: str) -> None:
    """Kept for callers/tests that only want the feed side."""
    record(level=DEFAULT_LEVEL, source="push-send", title=title, body=body)
