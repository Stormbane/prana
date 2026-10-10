"""The check-in tick mirrors a delivered nudge into the brain's notify
door (2026-10-07: nudges reached the box or Telegram, never the app)."""

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "akhada_checkin_tick.py"


def _load():
    spec = importlib.util.spec_from_file_location("akhada_checkin_tick", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_mirror_tells_the_door_telegram_is_done(tmp_path, monkeypatch):
    mod = _load()
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps({"prana": "tok"}), encoding="utf-8")
    monkeypatch.setattr(mod, "BRAIN_TOKENS", tokens)
    seen = {}

    def fake_urlopen(req, timeout=0):
        seen["body"] = json.loads(req.data.decode())
        seen["auth"] = req.headers.get("Authorization")
        return _Resp({"channels": ["badge", "push"], "push": {"sent": 1}})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    out = mod._notify_app("Evening check?", "akhada-checkin:2026-10-09:evening", True)
    assert seen["auth"] == "Bearer tok"
    assert seen["body"]["event_id"] == "akhada-checkin:2026-10-09:evening"
    assert seen["body"]["delivered_channels"] == ["telegram"]
    assert seen["body"]["level"] == "message"
    assert seen["body"]["url"] == "./#/today"  # the push opens today's cards
    assert "push" in out


def test_mirror_never_raises(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "BRAIN_TOKENS", tmp_path / "missing.json")
    assert mod._notify_app("x", "e", False).startswith("app: not mirrored")
