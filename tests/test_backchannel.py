"""Backchannel gating + clip loading (the mid-monologue "mm-hm").

The gate is pure logic with injected time; clips are plain WAVs whose
filenames are the karaoke text. No livekit needed here — the say()
delivery path is field-verified, this pins the decisions around it:
only real monologues get hummed at, pauses reset continuity, and bad
clip files degrade to "feature off", never to a crash.
"""

from __future__ import annotations

import random
import wave

import pytest

from prana.voice.backchannel import AckClip, BackchannelGate, load_clips


def _gate(**kw):
    kw.setdefault("min_speech_s", 8.0)
    kw.setdefault("interval_s", 12.0)
    kw.setdefault("jitter_s", 0.0)
    kw.setdefault("rng", random.Random(7))
    return BackchannelGate(**kw)


def test_no_ack_before_min_continuous_speech():
    g = _gate()
    g.on_user_state(True, 100.0)
    assert not g.should_ack(100.0)
    assert not g.should_ack(107.9)
    assert g.should_ack(108.0)


def test_pause_resets_continuity():
    g = _gate()
    g.on_user_state(True, 100.0)
    g.on_user_state(False, 105.0)   # he paused — not a monologue
    g.on_user_state(True, 106.0)
    assert not g.should_ack(108.0)  # only 2s into the NEW stretch
    assert g.should_ack(114.0)


def test_silence_never_acks():
    g = _gate()
    assert not g.should_ack(1000.0)
    g.on_user_state(False, 1000.0)
    assert not g.should_ack(2000.0)


def test_interval_between_acks():
    g = _gate()
    g.on_user_state(True, 100.0)
    assert g.should_ack(108.0)
    g.mark_acked(108.0)
    assert not g.should_ack(119.9)  # interval 12s, jitter 0
    assert g.should_ack(120.0)      # still the same monologue


def test_speaking_start_is_not_restamped():
    # A duplicate "speaking" event must not reset the continuity clock.
    g = _gate()
    g.on_user_state(True, 100.0)
    g.on_user_state(True, 107.0)
    assert g.should_ack(108.0)


def test_jitter_delays_next_ack():
    g = _gate(jitter_s=4.0, rng=random.Random(1))
    g.on_user_state(True, 0.0)
    g.mark_acked(10.0)
    # next ack lands in [22, 26); before 22 is always refused
    assert not g.should_ack(21.9)
    assert g.should_ack(26.0)


def test_pick_uses_all_clips_eventually():
    g = _gate(rng=random.Random(3))
    clips = [AckClip("a", b"", 24000), AckClip("b", b"", 24000)]
    seen = {g.pick(clips).text for _ in range(20)}
    assert seen == {"a", "b"}


# ── clip loading ─────────────────────────────────────────────────────

def _write_wav(path, *, channels=1, sampwidth=2, rate=24000,
               frames=b"\x00\x01" * 240):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(sampwidth)
        w.setframerate(rate)
        w.writeframes(frames)


def test_load_clips_reads_mono_16bit(tmp_path):
    _write_wav(tmp_path / "mm-hm.wav")
    _write_wav(tmp_path / "oh_right.wav", rate=16000)
    clips = load_clips(tmp_path)
    assert [(c.text, c.sample_rate) for c in clips] == [
        ("mm-hm", 24000), ("oh right", 16000)]
    assert clips[0].pcm == b"\x00\x01" * 240


def test_load_clips_skips_unusable(tmp_path):
    _write_wav(tmp_path / "stereo.wav", channels=2)
    (tmp_path / "garbage.wav").write_bytes(b"not a wav at all")
    _write_wav(tmp_path / "good.wav")
    clips = load_clips(tmp_path)
    assert [c.text for c in clips] == ["good"]


def test_load_clips_missing_dir_is_feature_off(tmp_path):
    assert load_clips(tmp_path / "nope") == []
