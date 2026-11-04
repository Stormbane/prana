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


# ── 2026-11-04: the door first, so a check-in reaches Telegram only as a fallback ──

import sys
import types
import urllib.error

import pytest


def test_door_sends_a_message_with_nothing_pre_delivered(tmp_path, monkeypatch):
    mod = _load()
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps({"prana": "tok"}), encoding="utf-8")
    monkeypatch.setattr(mod, "BRAIN_TOKENS", tokens)
    seen = {}

    def fake_urlopen(req, timeout=0):
        seen["body"] = json.loads(req.data.decode())
        return _Resp({"channels": ["badge", "push"], "push": {"sent": 1}, "telegram": None})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    out = mod._door("Midday check?", "akhada-checkin:2026-11-04:midday")
    assert seen["body"]["delivered_channels"] == [] and seen["body"]["level"] == "message"
    assert out["push"]["sent"] == 1


def test_door_unreachable_only_when_nothing_could_have_been_sent(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "BRAIN_TOKENS", tmp_path / "missing.json")
    with pytest.raises(mod.DoorUnreachable):
        mod._door("x", "e")
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps({"prana": "tok"}), encoding="utf-8")
    monkeypatch.setattr(mod, "BRAIN_TOKENS", tokens)

    def refused(req, timeout=0):
        raise urllib.error.URLError(ConnectionRefusedError(10061, "refused"))
    monkeypatch.setattr("urllib.request.urlopen", refused)
    with pytest.raises(mod.DoorUnreachable):
        mod._door("x", "e")

    def timed_out(req, timeout=0):
        raise urllib.error.URLError(TimeoutError("timed out"))
    monkeypatch.setattr("urllib.request.urlopen", timed_out)
    with pytest.raises(urllib.error.URLError):     # ambiguous: not "unreachable"
        mod._door("x", "e")


def _fake_akhada(monkeypatch, record):
    checkin = types.ModuleType("akhada.checkin")
    checkin.build_checkin = lambda store: {"due": True, "slot": "midday", "day": "2026-11-04",
                                           "text": "Midday check-in"}
    checkin.claim_checkin = lambda store, slot, day: True
    checkin.mark_sending = lambda store, slot, day: record.append("sending")
    checkin.confirm_checkin = lambda store, slot, day, channel, text: record.append(f"confirm:{channel}")
    checkin.release_checkin = lambda store, slot, day: record.append("release")
    db = types.ModuleType("akhada.store.db")
    db.Store = lambda: object()
    router = types.ModuleType("prana.state.router")
    router.route_utterance = lambda *a, **k: types.SimpleNamespace(
        ok=True, delivered_to="telegram:1", body_error=None, telegram_error=None)
    for name, m in {"akhada": types.ModuleType("akhada"), "akhada.checkin": checkin,
                    "akhada.store": types.ModuleType("akhada.store"), "akhada.store.db": db,
                    "prana.state.router": router}.items():
        monkeypatch.setitem(sys.modules, name, m)


@pytest.mark.parametrize("door,expect,rc", [
    ({"push": {"sent": 1}, "telegram": None}, "confirm:app", 0),            # the app got it: no Telegram
    ({"push": {"sent": 0}, "telegram": True}, "confirm:telegram", 0),       # no device: the fallback
    ({"push": {"sent": 0}, "telegram": False}, "release", 1),               # nothing arrived
])
def test_tick_records_where_the_door_delivered(monkeypatch, door, expect, rc):
    mod = _load()
    record = []
    _fake_akhada(monkeypatch, record)
    monkeypatch.setattr(mod, "_talk_page_up", lambda: False)
    monkeypatch.setattr(mod, "datetime", types.SimpleNamespace(now=lambda: types.SimpleNamespace(hour=11)))
    monkeypatch.setattr(mod, "_door", lambda text, event_id: door)
    assert mod.main() == rc
    assert record[-1] == expect


def test_tick_routes_directly_only_when_the_brain_is_unreachable(monkeypatch):
    mod = _load()
    record = []
    _fake_akhada(monkeypatch, record)
    monkeypatch.setattr(mod, "_talk_page_up", lambda: False)
    monkeypatch.setattr(mod, "datetime", types.SimpleNamespace(now=lambda: types.SimpleNamespace(hour=11)))

    def unreachable(text, event_id):
        raise mod.DoorUnreachable("connection refused")
    monkeypatch.setattr(mod, "_door", unreachable)
    monkeypatch.setattr(mod, "_notify_app", lambda *a, **k: "app: not mirrored")
    assert mod.main() == 0 and record[-1] == "confirm:telegram:1"


def test_an_ambiguous_door_failure_keeps_the_claim(monkeypatch):
    mod = _load()
    record = []
    _fake_akhada(monkeypatch, record)
    monkeypatch.setattr(mod, "_talk_page_up", lambda: False)
    monkeypatch.setattr(mod, "datetime", types.SimpleNamespace(now=lambda: types.SimpleNamespace(hour=11)))

    def timed_out(text, event_id):
        raise TimeoutError("after the request went out")
    monkeypatch.setattr(mod, "_door", timed_out)
    assert mod.main() == 1 and "release" not in record and "confirm" not in "".join(record)
