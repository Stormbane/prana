"""API credits (Suti 2026-11-05): a balance where the API has one, the spend
where only that exists, an honest "no balance by API" otherwise."""
import json

import pytest

from prana import credits


def fake(responses):
    calls = []

    def get(url, headers):
        calls.append((url, headers))
        for prefix, body in responses.items():
            if url.startswith(prefix):
                if isinstance(body, Exception):
                    raise body
                return body
        raise AssertionError(f"unexpected call {url}")
    get.calls = calls
    return get


@pytest.fixture(autouse=True)
def _ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("NARADA_GEMINI_LEDGER", str(tmp_path / "ledger.json"))
    monkeypatch.delenv("NARADA_GEMINI_MONTH_CAP_USD", raising=False)
    monkeypatch.delenv("VOICE_LAB_GEMINI_MONTH_CAP_USD", raising=False)


def test_each_provider_reports_what_it_honestly_can(tmp_path):
    get = fake({
        "https://api.fal.ai/v1/account/billing": {"credits": {"current_balance": 24.5, "currency": "USD"}},
        "https://openrouter.ai/api/v1/key": {"data": {"limit_remaining": None, "usage_monthly": 3.2}},
    })
    keys = {"FAL_KEY": "k1", "OPENROUTER_API_KEY": "k2", "OPENAI_API_KEY": "k3"}
    keys["NARADA_GEMINI_LEDGER"] = str(tmp_path / "ledger.json")
    out = credits.run(get, keys, path=tmp_path / "credits.json")
    rows = {r["provider"]: r for r in out["providers"]}
    assert rows["fal"]["balance_usd"] == 24.5 and rows["fal"]["status"] == "ok"
    assert rows["openrouter"]["spent_usd"] == 3.2 and rows["openrouter"]["balance_usd"] is None
    assert "management key" in rows["openrouter"]["note"]
    assert rows["openai"]["status"] == "unknown" and "no balance by API" in rows["openai"]["note"]
    assert "subscription" in rows["anthropic"]["note"]
    assert rows["gemini"]["limit_usd"] == 10.0 and rows["gemini"]["spent_usd"] == 0
    # fal's key travels as "Key <k>", OpenRouter's as Bearer; no network for the rest
    assert {u.split("?")[0] for u, _ in get.calls} == {
        "https://api.fal.ai/v1/account/billing", "https://openrouter.ai/api/v1/key"}


def test_keys_never_reach_the_file(tmp_path):
    get = fake({"https://api.fal.ai/": credits.HttpStatus(500),
                "https://openrouter.ai/api/v1/key": {"data": {"limit_remaining": 1.5, "usage_monthly": 9}}})
    credits.run(get, {"FAL_KEY": "secret-fal-key", "OPENROUTER_API_KEY": "secret-or-key"}, path=tmp_path / "c.json")
    text = (tmp_path / "c.json").read_text(encoding="utf-8")
    assert "secret" not in text
    rows = {r["provider"]: r for r in json.loads(text)["providers"]}
    assert rows["fal"]["status"] == "error" and rows["fal"]["note"] == "HTTP 500"
    assert rows["openrouter"]["status"] == "low"   # a capped key with $1.50 left


def test_a_fal_key_without_billing_scope_says_so(tmp_path):
    get = fake({"https://api.fal.ai/": credits.HttpStatus(403)})
    out = credits.run(get, {"FAL_KEY": "k"}, path=tmp_path / "c.json")
    fal = next(r for r in out["providers"] if r["provider"] == "fal")
    assert fal["status"] == "unknown" and "billing:usage:read" in fal["note"]


def test_openai_spend_with_an_admin_key_sums_the_month(tmp_path):
    get = fake({"https://api.openai.com/v1/organization/costs": {
        "data": [{"results": [{"amount": {"value": 0.13}}, {"amount": {"value": 1.0}}]}], "has_more": False}})
    out = credits.run(get, {"OPENAI_ADMIN_KEY": "admin"}, path=tmp_path / "c.json")
    oa = next(r for r in out["providers"] if r["provider"] == "openai")
    assert oa["spent_usd"] == 1.13 and oa["period"] == "this month"


def test_latest_marks_an_old_check_stale(tmp_path):
    path = tmp_path / "c.json"
    credits.run(fake({}), {}, path=path)
    assert credits.latest(path)["stale"] is False
    assert credits.latest(path, now=__import__("time").time() + 40 * 3600)["stale"] is True


def test_gemini_reads_the_cap_and_ledger_the_worker_uses(tmp_path):
    """Review: a cap set in the env files must reach this check too."""
    from prana.voice.gemini.core import Ledger
    ledger = tmp_path / "voice-ledger.json"
    Ledger(ledger).add("call", 1.25)
    keys = {"NARADA_GEMINI_LEDGER": str(ledger), "VOICE_LAB_GEMINI_MONTH_CAP_USD": "20"}
    out = credits.run(fake({}), keys, path=tmp_path / "c.json")
    g = next(r for r in out["providers"] if r["provider"] == "gemini")
    assert g["limit_usd"] == 20.0 and g["spent_usd"] == 1.25 and g["balance_usd"] == 18.75


def test_keys_follow_the_workers_precedence(tmp_path, monkeypatch):
    """Review: the environment wins, then .voice.env, then .keys.env."""
    home = tmp_path / "home"
    (home / ".narada").mkdir(parents=True)
    (home / ".narada" / ".voice.env").write_text("OPENROUTER_API_KEY=from-voice\nFAL_KEY=fal-voice\n", encoding="utf-8")
    (home / ".narada" / ".keys.env").write_text("OPENROUTER_API_KEY=from-keys\nFAL_KEY=fal-keys\nGOOGLE_API_KEY=g\n", encoding="utf-8")
    monkeypatch.setattr(credits.Path, "home", lambda: home)
    monkeypatch.setenv("FAL_KEY", "fal-env")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    keys = credits.load_keys()
    assert keys["FAL_KEY"] == "fal-env"
    assert keys["OPENROUTER_API_KEY"] == "from-voice"
    assert keys["GOOGLE_API_KEY"] == "g"
