"""Rest timers ring in-session, personal tier only (akhada gym-session
plan G4). The factory tests without livekit; the tier gating rides the
same build path as the other personal tools."""

from __future__ import annotations

import asyncio

import pytest

from prana.voice import tools as tools_mod
from prana.voice.tools import make_rest_timer

from test_voice_akhada_pack import _build, _names  # noqa: F401  (fixture)


def test_rest_timer_personal_tier_only(_build):  # noqa: F811
    assert "set_rest_timer" not in _names(_build("shareable"))
    personal = _names(_build("personal"))
    assert {"set_rest_timer", "cancel_rest_timer"} <= personal


def test_ring_speaks_in_session(monkeypatch):
    monkeypatch.setattr(tools_mod, "REST_MIN_S", 0.0)
    spoken: list[str] = []

    async def speak(inst: str) -> None:
        spoken.append(inst)

    async def go():
        set_rest, _ = make_rest_timer(speak)
        r = await set_rest(0.01, "bench")
        assert r["set"]
        await asyncio.sleep(0.1)

    asyncio.run(go())
    assert len(spoken) == 1 and "bench" in spoken[0]


def test_new_timer_replaces_old_and_cancel_cancels(monkeypatch):
    monkeypatch.setattr(tools_mod, "REST_MIN_S", 0.0)
    spoken: list[str] = []

    async def speak(inst: str) -> None:
        spoken.append(inst)

    async def go():
        set_rest, cancel_rest = make_rest_timer(speak)
        await set_rest(10, "first")          # would ring far in the future
        r = await set_rest(0.01, "second")   # replaces it
        assert r["set"]
        await asyncio.sleep(0.1)
        assert [s for s in spoken if "second" in s]
        assert not [s for s in spoken if "first" in s]
        await set_rest(10, "third")
        c = await cancel_rest()
        assert c["cancelled"]
        c2 = await cancel_rest()
        assert not c2["cancelled"]

    asyncio.run(go())
    assert len(spoken) == 1


def test_bounds_and_no_voice_surface():
    async def go():
        set_rest, _ = make_rest_timer(None)  # a surface with no live voice
        r = await set_rest(90)
        assert not r["set"] and "set_timer" in r["reason"]

        async def speak(_):  # pragma: no cover - never rings here
            pass
        set_rest2, _ = make_rest_timer(speak)
        assert not (await set_rest2(1))["set"]      # under the floor
        assert not (await set_rest2(9999))["set"]   # over the ceiling
        assert not (await set_rest2("soon"))["set"]

    asyncio.run(go())
