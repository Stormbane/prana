"""A2 — the health shim must tell the truth (resilience-and-reach).

The lesson being encoded: the framework's own probe served 200 for a
week while LiveKit was down. The shim's verdict is 200 ONLY when every
dependency probe passes, and the 503 body names what failed.
"""

from __future__ import annotations

import asyncio

import pytest

from prana.voice.worker import _health_verdict


def _ok():
    async def probe():
        return None
    return probe


def _fail(reason: str):
    async def probe():
        return reason
    return probe


def test_all_probes_pass_is_200():
    status, body = asyncio.run(_health_verdict(probes=(_ok(), _ok())))
    assert status == 200
    assert body == "OK"


def test_livekit_down_is_503_even_if_agents_up():
    status, body = asyncio.run(_health_verdict(
        probes=(_ok(), _fail("livekit unreachable: ConnectionError"))))
    assert status == 503
    assert "livekit unreachable" in body


def test_agents_down_is_503():
    status, body = asyncio.run(_health_verdict(
        probes=(_fail("agents server unreachable: TimeoutError"), _ok())))
    assert status == 503
    assert "agents server" in body


def test_multiple_failures_all_named():
    status, body = asyncio.run(_health_verdict(
        probes=(_fail("agents server status 500"),
                _fail("livekit unreachable: TimeoutError"))))
    assert status == 503
    assert "agents server status 500" in body
    assert "livekit unreachable" in body


def test_probe_exception_does_not_fake_health():
    # A probe that raises must not be swallowed into a 200 — the shim
    # composes reasons, it does not guess. gather() propagates; the
    # handler layer turns any unhandled error into a failed request,
    # which the supervisor reads as unhealthy. Verify propagation.
    async def boom():
        raise RuntimeError("probe bug")

    with pytest.raises(RuntimeError):
        asyncio.run(_health_verdict(probes=(boom, _ok())))
