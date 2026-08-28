"""B2 — message_suti: personal tier only, durable cap, honest failures."""

from __future__ import annotations

from pathlib import Path

import pytest

from prana.state.router import RouteResult
from prana.voice import messaging


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def fake_route_ok(text, **kw):
    fake_route_ok.sent.append(text)
    return RouteResult(1, "telegram:123", None, False, None, True, None)


def fake_route_fail(text, **kw):
    return RouteResult(1, None, None, False, None, True, "ConnectionError")


def test_shareable_tier_refused_in_code(tmp_path: Path):
    with pytest.raises(messaging.NotAllowed):
        messaging.send_to_suti("hi", tier="shareable", session_id="s",
                               db_path=tmp_path / "s.db")


def test_shareable_surface_has_no_tool(tmp_path: Path):
    from prana.sessions.escalate import ProposalQueue
    from prana.sessions.service import ServiceClient
    from prana.voice.tools import build_voice_tools

    def names(tier):
        tools = build_voice_tools(
            client=ServiceClient(port=1, token="unused"),
            proposals=ProposalQueue(tmp_path / "p.db"),
            tier=tier)
        out = set()
        for t in tools:
            info = getattr(t, "info", None) or getattr(t, "_info", None)
            out.add(getattr(info, "name", None) or getattr(t, "__name__", None))
        return out

    assert "message_suti" not in names("shareable")
    assert "message_suti" in names("personal")


def test_rate_cap_is_durable_and_counts_attempts(tmp_path: Path):
    db = tmp_path / "s.db"
    clock = Clock()
    fake_route_ok.sent = []
    for _ in range(messaging.MESSAGES_PER_HOUR - 1):
        messaging.send_to_suti("ping", tier="personal", session_id="s",
                               db_path=db, now=clock, route=fake_route_ok)
    # A FAILING attempt still consumes quota.
    messaging.send_to_suti("ping", tier="personal", session_id="s",
                           db_path=db, now=clock, route=fake_route_fail)
    with pytest.raises(messaging.RateLimited):
        messaging.send_to_suti("over", tier="personal", session_id="s",
                               db_path=db, now=clock, route=fake_route_ok)
    # "Worker restart" = fresh call stack, same db: still capped.
    with pytest.raises(messaging.RateLimited):
        messaging.send_to_suti("still over", tier="personal",
                               session_id="s", db_path=db, now=clock,
                               route=fake_route_ok)
    # The window slides.
    clock.t += 3601
    r = messaging.send_to_suti("later", tier="personal", session_id="s",
                               db_path=db, now=clock, route=fake_route_ok)
    assert r["delivered"] is True


def test_attribution_and_redaction(tmp_path: Path):
    fake_route_ok.sent = []
    messaging.send_to_suti("key sk-abcdefghijklmnop123456", tier="personal",
                           session_id="s", db_path=tmp_path / "s.db",
                           route=fake_route_ok)
    sent = fake_route_ok.sent[0]
    assert sent.startswith(messaging.ORIGIN_PREFIX)
    assert "sk-abcdefghijklmnop123456" not in sent


def test_delivery_failure_surfaces(tmp_path: Path):
    r = messaging.send_to_suti("hi", tier="personal", session_id="s",
                               db_path=tmp_path / "s.db",
                               route=fake_route_fail)
    assert r["delivered"] is False
    assert "ConnectionError" in r["detail"]
