"""GET /api/doctor: one run a minute however often it's asked, tiers
limited, and a failed run never blanks a card already shown."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from prana.brain.doctor_api import register_doctor
from prana.doctor.model import GREEN, RED, CheckResult, Report


async def _tier(request: Request) -> str:
    tier = request.headers.get("x-tier")
    if not tier:
        raise HTTPException(401, "no token")
    return tier


def _report(status=GREEN) -> Report:
    return Report(generated_at=datetime(2026, 11, 4, 4, 0, tzinfo=timezone.utc), host="pc",
                  checks=[CheckResult(id="push_subs", title="Phone push subscriptions",
                                      status=status, reason="1 device")])


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def world():
    runs, clock = [], Clock()

    def run():
        runs.append(clock.t)
        if getattr(run, "fail", False):
            raise RuntimeError("probe exploded")
        return _report(getattr(run, "status", GREEN))

    app = FastAPI()
    register_doctor(app, _tier, run=run, clock=clock)
    return TestClient(app), runs, clock, run


def test_card_is_served_and_cached_for_a_minute(world):
    client, runs, clock, _ = world
    card = client.get("/api/doctor", headers={"x-tier": "app"}).json()
    assert card["kind"] == "narada.status-card" and card["overall"] == GREEN
    clock.t += 30
    client.get("/api/doctor", headers={"x-tier": "app"})
    assert len(runs) == 1
    clock.t += 31
    client.get("/api/doctor", headers={"x-tier": "app"})
    assert len(runs) == 2


def test_fresh_reruns_but_not_more_than_every_ten_seconds(world):
    client, runs, clock, _ = world
    client.get("/api/doctor", headers={"x-tier": "app"})
    clock.t += 5
    client.get("/api/doctor?fresh=1", headers={"x-tier": "app"})
    assert len(runs) == 1
    clock.t += 6
    client.get("/api/doctor?fresh=1", headers={"x-tier": "app"})
    assert len(runs) == 2


def test_voice_tier_and_no_token_are_refused(world):
    client, runs, _, _ = world
    assert client.get("/api/doctor", headers={"x-tier": "voice"}).status_code == 403
    assert client.get("/api/doctor").status_code == 401
    assert runs == []


def test_a_failed_run_keeps_the_last_card(world):
    client, runs, clock, run = world
    run.status = RED
    first = client.get("/api/doctor", headers={"x-tier": "prana"}).json()
    run.fail = True
    clock.t += 61
    again = client.get("/api/doctor", headers={"x-tier": "prana"})
    assert again.status_code == 200 and again.json() == first  # dated by generated_at


def test_a_failed_first_run_is_a_502(world):
    client, _, _, run = world
    run.fail = True
    res = client.get("/api/doctor", headers={"x-tier": "app"})
    assert res.status_code == 502 and "RuntimeError" in res.json()["detail"]
