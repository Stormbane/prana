"""Tap-admission protocol — adversarial (M2 spec §2.2).

If any of these fail, someone other than the box (or a replayed tap)
can unlock a session tier. Security tests, not style.
"""

from __future__ import annotations

import json

from prana.voice.admission import (
    DEVICE_IDENTITY,
    TIER_PERSONAL,
    TIER_SHAREABLE,
    TapAdmission,
    is_sleep_tap,
)


def _tap(nonce, tier="personal"):
    return json.dumps({"type": "tap", "nonce": nonce, "tier": tier})


def test_happy_path_personal():
    a = TapAdmission()
    nonce = a.new_cycle()
    result = a.verify(DEVICE_IDENTITY, _tap(nonce, "personal"))
    assert result is not None and result.tier == TIER_PERSONAL


def test_wrong_sender_rejected():
    a = TapAdmission()
    nonce = a.new_cycle()
    assert a.verify("evil-participant", _tap(nonce)) is None
    assert a.verify(None, _tap(nonce)) is None
    # and the nonce was NOT consumed by the failed attempts
    assert a.verify(DEVICE_IDENTITY, _tap(nonce)) is not None


def test_nonce_is_one_shot():
    a = TapAdmission()
    nonce = a.new_cycle()
    assert a.verify(DEVICE_IDENTITY, _tap(nonce)) is not None
    # replay of the same valid packet
    assert a.verify(DEVICE_IDENTITY, _tap(nonce)) is None


def test_stale_nonce_rejected_after_new_cycle():
    a = TapAdmission()
    old = a.new_cycle()
    a.new_cycle()  # new cycle invalidates old nonce
    assert a.verify(DEVICE_IDENTITY, _tap(old)) is None


def test_no_open_cycle_rejects():
    a = TapAdmission()
    assert a.verify(DEVICE_IDENTITY, _tap("anything")) is None
    nonce = a.new_cycle()
    a.invalidate()
    assert a.verify(DEVICE_IDENTITY, _tap(nonce)) is None


def test_wrong_or_missing_nonce_rejected():
    a = TapAdmission()
    a.new_cycle()
    assert a.verify(DEVICE_IDENTITY, _tap("forged-nonce")) is None
    assert a.verify(DEVICE_IDENTITY, json.dumps({"type": "tap"})) is None
    assert a.verify(DEVICE_IDENTITY, json.dumps({"type": "tap", "nonce": 42})) is None


def test_unknown_tier_defaults_shareable():
    a = TapAdmission()
    nonce = a.new_cycle()
    result = a.verify(DEVICE_IDENTITY, _tap(nonce, tier="root"))
    assert result is not None and result.tier == TIER_SHAREABLE


def test_malformed_payloads_never_raise():
    a = TapAdmission()
    a.new_cycle()
    for garbage in (b"\xff\xfe", "not json", "[]", json.dumps({"type": "x"}),
                    b"", "{}", json.dumps(["tap"])):
        assert a.verify(DEVICE_IDENTITY, garbage) is None


def test_sleep_tap_sender_checked():
    payload = json.dumps({"type": "tap"})
    assert is_sleep_tap(DEVICE_IDENTITY, payload) is True
    assert is_sleep_tap("evil", payload) is False
    assert is_sleep_tap(DEVICE_IDENTITY, "junk") is False
