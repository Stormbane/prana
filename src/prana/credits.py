"""API credits, checked once a night (Suti 2026-11-05: "show all our API
credits... query each of these APIs to get the credit left... once a night,
as a part of the sleep cycle").

Narada_Credits runs this after smriti's nightly. Each provider is asked in
the cheapest honest way it allows: a balance where the API reports one, the
month's spend where only that exists, and "not available by API" otherwise,
never a guess. Gemini comes from our own ledger (exact, no network). Keys are
read from ~/.narada's env files and never logged or written out.

The result lands in ~/.narada/brain/credits.json; the brain serves it at
GET /api/credits for the Data tab.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

LOW_USD = 2.0          # below this a balance reads "low"
TIMEOUT_S = 15

Getter = Callable[[str, dict[str, str]], Any]   # (url, headers) -> parsed JSON


def credits_path() -> Path:
    return Path(os.environ.get("NARADA_CREDITS_PATH")
                or Path.home() / ".narada" / "brain" / "credits.json")


def load_keys() -> dict[str, str]:
    """Settings and keys with the voice worker's precedence: the process
    environment first, then .voice.env, then .keys.env (each filling only
    what is still missing, as load_dotenv does). Values stay in memory."""
    from dotenv import dotenv_values  # the worker's own parser: comments, quotes, ${...}

    keys: dict[str, str] = {k: v for k, v in os.environ.items() if v}
    for name in (".voice.env", ".keys.env"):
        path = Path.home() / ".narada" / name
        if not path.is_file():
            continue
        for k, v in dotenv_values(path).items():
            if v and not v.startswith("PASTE_"):
                keys.setdefault(k, v)
    return keys


def _gemini_setting(keys: dict[str, str], name: str, default: str) -> str:
    """A Gemini setting as the talker resolves it (NARADA_GEMINI_*, then the
    lab's VOICE_LAB_GEMINI_*), from the same sources as the worker."""
    return keys.get(f"NARADA_GEMINI_{name}") or keys.get(f"VOICE_LAB_GEMINI_{name}") or default


class HttpStatus(Exception):
    """A provider answered with an HTTP error (403: the key can't see billing)."""

    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status = status


def http_json(url: str, headers: dict[str, str]) -> Any:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "narada-credits/1", **headers})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:  # noqa: S310 - fixed https URLs
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # never echo the body: it can quote the key
        raise HttpStatus(exc.code) from None


def _row(provider: str, label: str, **kw: Any) -> dict[str, Any]:
    return {"provider": provider, "label": label, "status": kw.pop("status", "unknown"),
            "balance_usd": kw.pop("balance_usd", None), "spent_usd": kw.pop("spent_usd", None),
            "limit_usd": kw.pop("limit_usd", None), "period": kw.pop("period", None),
            "note": kw.pop("note", ""), "source": kw.pop("source", "")}


def _status_for_balance(balance: float | None) -> str:
    if balance is None:
        return "unknown"
    if balance <= 0:
        return "empty"
    return "low" if balance < LOW_USD else "ok"


def gemini(keys: dict[str, str], get: Getter) -> dict[str, Any]:
    """Our own ledger: the month's spend against Suti's cap, exactly."""
    try:
        from prana.voice.gemini.core import Ledger, month_start
        ledger = Path(_gemini_setting(keys, "LEDGER", str(Path.home() / ".narada" / "voice" / "gemini-trial-ledger.json")))
        spent = Ledger(ledger).spent(since=month_start())
    except Exception as exc:  # noqa: BLE001 - reported, never guessed
        return _row("gemini", "Gemini (voice)", status="error", note=f"ledger unreadable: {type(exc).__name__}")
    cap = float(_gemini_setting(keys, "MONTH_CAP_USD", "10.00"))
    left = cap - spent
    return _row("gemini", "Gemini (voice)", status=_status_for_balance(left), spent_usd=round(spent, 4),
                limit_usd=cap, balance_usd=round(left, 4), period="this month",
                note="our cap, from Narada's own ledger (Google reports no balance by API)",
                source="ledger")


def openrouter(keys: dict[str, str], get: Getter) -> dict[str, Any]:
    """The account balance needs a management key; a normal key reports
    its own spend (and its cap, if it has one)."""
    mgmt = keys.get("OPENROUTER_MANAGEMENT_KEY")
    if mgmt:
        d = get("https://openrouter.ai/api/v1/credits", {"Authorization": f"Bearer {mgmt}"})["data"]
        bal = float(d["total_credits"]) - float(d["total_usage"])
        return _row("openrouter", "OpenRouter", status=_status_for_balance(bal), balance_usd=round(bal, 2),
                    spent_usd=round(float(d["total_usage"]), 2), source="credits")
    key = keys.get("OPENROUTER_API_KEY")
    if not key:
        return _row("openrouter", "OpenRouter", note="no key on this PC")
    d = get("https://openrouter.ai/api/v1/key", {"Authorization": f"Bearer {key}"})["data"]
    left = d.get("limit_remaining")
    return _row("openrouter", "OpenRouter",
                status=_status_for_balance(float(left)) if left is not None else "unknown",
                balance_usd=round(float(left), 2) if left is not None else None,
                spent_usd=round(float(d.get("usage_monthly") or 0), 2), period="this month",
                note="this key's spend; the account balance needs a management key",
                source="key")


def fal(keys: dict[str, str], get: Getter) -> dict[str, Any]:
    key = keys.get("FAL_KEY")
    if not key:
        return _row("fal", "fal.ai", note="no key on this PC")
    try:
        d = get("https://api.fal.ai/v1/account/billing?expand=credits", {"Authorization": f"Key {key}"})
    except HttpStatus as exc:
        if exc.status in (401, 403):
            return _row("fal", "fal.ai", note="this key can't read billing (needs billing:usage:read)")
        raise
    bal = float(d["credits"]["current_balance"])
    return _row("fal", "fal.ai", status=_status_for_balance(bal), balance_usd=round(bal, 2), source="billing")


def openai(keys: dict[str, str], get: Getter) -> dict[str, Any]:
    """OpenAI has no balance API; an admin key gives the month's spend."""
    admin = keys.get("OPENAI_ADMIN_KEY")
    if not admin:
        return _row("openai", "OpenAI",
                    note="no balance by API; an admin key would show the month's spend")
    start = int(_month_start())
    total, page = 0.0, None
    for _ in range(12):   # a month of daily buckets is one or two pages
        url = f"https://api.openai.com/v1/organization/costs?start_time={start}&bucket_width=1d&limit=31"
        d = get(url + (f"&page={page}" if page else ""), {"Authorization": f"Bearer {admin}"})
        for bucket in d.get("data", []):
            for res in bucket.get("results", []):
                total += float((res.get("amount") or {}).get("value") or 0)
        if not d.get("has_more"):
            break
        page = d.get("next_page")
    return _row("openai", "OpenAI", spent_usd=round(total, 2), period="this month",
                note="spend only: OpenAI reports no balance", source="costs")


def anthropic(keys: dict[str, str], get: Getter) -> dict[str, Any]:
    if not keys.get("ANTHROPIC_ADMIN_KEY"):
        return _row("anthropic", "Anthropic",
                    note="Claude runs on the subscription here; no API credit to check")
    return _row("anthropic", "Anthropic", note="an admin key is set; the cost report isn't wired yet")


def _month_start() -> float:
    from prana.voice.gemini.core import month_start
    return month_start()


CHECKS: dict[str, Callable[[dict[str, str], Getter], dict[str, Any]]] = {
    "fal": fal,
    "openrouter": openrouter,
    "gemini": gemini,
    "openai": openai,
    "anthropic": anthropic,
}


def run(get: Getter = http_json, keys: dict[str, str] | None = None,
        path: Path | None = None) -> dict[str, Any]:
    keys = load_keys() if keys is None else keys
    rows = []
    for name, check in CHECKS.items():
        try:
            rows.append(check(keys, get))
        except Exception as exc:  # noqa: BLE001 - one provider never sinks the rest
            # the status code or the error's type, never its text (it can quote a key)
            note = str(exc) if isinstance(exc, HttpStatus) else type(exc).__name__
            rows.append(_row(name, name, status="error", note=note[:120]))
    out = {"checked_at": datetime.now().astimezone().isoformat(), "providers": rows}
    path = path or credits_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(out, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return out


def latest(path: Path | None = None, stale_after_h: float = 30.0, now: float | None = None) -> dict[str, Any]:
    try:
        data = json.loads((path or credits_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"checked_at": None, "providers": [], "stale": True}
    try:
        age_h = ((now or time.time()) - datetime.fromisoformat(data["checked_at"]).timestamp()) / 3600
    except (KeyError, ValueError, TypeError):
        age_h = 1e9
    return {**data, "stale": age_h > stale_after_h}


def main() -> int:
    out = run()
    return 0 if all(r["status"] != "error" for r in out["providers"]) else 3


if __name__ == "__main__":
    raise SystemExit(main())
