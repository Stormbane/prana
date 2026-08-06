"""The heartbeat's CHECK_IN and SPEAK paths must work without prana.bus.

The bus package was removed in the embodiment-rebirth Phase 0 cleanup;
these paths now route via prana.state.router.route_utterance directly.
The bus-absence simulation (sys.modules poisoning) keeps the guarantee
honest even if a bus/ package ever reappears in the tree.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import prana.heartbeat.daemon as daemon_mod
import prana.state.router as router_mod
from prana.heartbeat.daemon import HeartbeatDaemon
from prana.heartbeat.viveka import Action, Desire
from prana.state.router import RouteResult


BUS_MODULES = ("prana.bus", "prana.bus.actions", "prana.bus.actions.speak")


@pytest.fixture
def no_bus(monkeypatch):
    """Make any import of prana.bus fail, as if the package is absent."""
    for name in BUS_MODULES:
        monkeypatch.setitem(sys.modules, name, None)


@pytest.fixture
def routed(monkeypatch):
    """Stub route_utterance; capture calls."""
    calls = []

    def fake_route(text, *, source, topic="", priority=0, force_channel=None):
        calls.append(
            {"text": text, "source": source, "topic": topic, "priority": priority}
        )
        return RouteResult(
            utterance_id=42,
            delivered_to="telegram:suti",
            skipped_reason=None,
            body_attempted=False,
            body_error=None,
            telegram_attempted=True,
            telegram_error=None,
        )

    monkeypatch.setattr(router_mod, "route_utterance", fake_route)
    return calls


@pytest.fixture
def stub_daemon(monkeypatch, tmp_path):
    """A bare object carrying only what the handlers touch on self."""
    monkeypatch.setattr(daemon_mod, "MESSAGES_DIR", tmp_path / "messages")
    monkeypatch.setattr(daemon_mod, "_load_smtp_config", lambda: None)
    cycles = []
    stub = SimpleNamespace(
        display=SimpleNamespace(set_status=lambda *_: None),
        _log_cycle=lambda record, now=None: cycles.append(record),
    )
    stub.logged_cycles = cycles
    return stub


def _desire(action: Action, reason: str = "hello Suti", topic: str = "greeting"):
    return Desire(action=action, topic=topic, reason=reason, raw_response="{}")


def test_speak_routes_without_bus(no_bus, routed, stub_daemon):
    result = HeartbeatDaemon._handle_speak(
        stub_daemon,
        _desire(Action.SPEAK),
        datetime.now(timezone.utc),
        0.0,
    )
    assert result["approved"] is True
    assert routed and routed[0]["priority"] == 1
    assert stub_daemon.logged_cycles[0].approved is True


def test_check_in_routes_without_bus(no_bus, routed, stub_daemon):
    result = HeartbeatDaemon._handle_check_in(
        stub_daemon,
        _desire(Action.CHECK_IN),
        datetime.now(timezone.utc),
        0.0,
    )
    assert result["approved"] is True
    assert routed and routed[0]["priority"] == 2
    # message file written to the patched dir, not the real ~/.narada
    assert result["email_sent"] is False


def test_bus_really_absent_from_tree():
    """The package itself must be gone, not just unused."""
    import importlib.util

    assert importlib.util.find_spec("prana.bus") is None
