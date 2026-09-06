"""prana.cast — the pure parts: speaker resolution, content types, and
the play/stop decision paths with a faked cast layer. Discovery and
actual playback are field-verified (real speakers, real mDNS); these
pin the manners around them."""

from __future__ import annotations

import pytest

from prana import cast as cast_mod
from prana.cast import _content_type, resolve_speaker


# ── content types ────────────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://x/stream.mp3", "audio/mpeg"),
    ("https://x/hq/master.m3u8", "application/x-mpegurl"),
    ("https://x/master.m3u8?token=abc", "application/x-mpegurl"),
    ("https://x/radio.aac", "audio/aac"),
    ("https://x/radio.ogg", "audio/ogg"),
    ("https://x/2FMW/mp3/", "audio/mpeg"),   # extensionless stream
])
def test_content_type(url, expected):
    assert _content_type(url) == expected


# ── speaker resolution ───────────────────────────────────────────────

NAMES = ["Kitchen speaker", "Bedroom", "Everywhere"]


def test_resolve_exact_beats_substring():
    assert resolve_speaker("bedroom", NAMES) == "Bedroom"


def test_resolve_substring():
    assert resolve_speaker("kitchen", NAMES) == "Kitchen speaker"


def test_resolve_miss():
    assert resolve_speaker("garage", NAMES) is None


def test_empty_query_single_speaker_wins():
    assert resolve_speaker("", ["Only one"]) == "Only one"


def test_empty_query_many_speakers_is_ambiguous(monkeypatch):
    monkeypatch.delenv(cast_mod.DEFAULT_ENV, raising=False)
    assert resolve_speaker("", NAMES) is None


def test_empty_query_uses_default_env(monkeypatch):
    monkeypatch.setenv(cast_mod.DEFAULT_ENV, "everywhere")
    assert resolve_speaker("", NAMES) == "Everywhere"


# ── play/stop paths with a faked cast layer ──────────────────────────

class _FakeMC:
    def __init__(self):
        self.played = None

    def play_media(self, url, ctype, title=None, stream_type=None):
        self.played = (url, ctype, stream_type)

    def block_until_active(self, timeout=None):
        pass


class _FakeCast:
    def __init__(self, name):
        self.name = name
        self.model_name = "Fake"
        self.cast_type = "audio"
        self.media_controller = _FakeMC()
        self.quit = False

    def wait(self, timeout=None):
        pass

    def quit_app(self):
        self.quit = True


@pytest.fixture
def fake_house(monkeypatch):
    casts = [_FakeCast("Kitchen speaker"), _FakeCast("Bedroom")]
    monkeypatch.setattr(cast_mod, "_discover", lambda refresh=False: casts)
    monkeypatch.setattr(cast_mod, "_active", set())
    return casts


def test_play_resolves_station_and_speaker(fake_house, monkeypatch):
    from prana.voice import music
    monkeypatch.setattr(music, "load_stations",
                        lambda path=None: {"Groove Salad":
                                           "https://x/gs.mp3"})
    out = cast_mod.play("groove", "kitchen")
    assert out == {"ok": True, "station": "Groove Salad",
                   "speaker": "Kitchen speaker"}
    url, ctype, stream_type = fake_house[0].media_controller.played
    assert (url, ctype, stream_type) == (
        "https://x/gs.mp3", "audio/mpeg", "LIVE")


def test_play_unknown_station_lists_options(fake_house, monkeypatch):
    from prana.voice import music
    monkeypatch.setattr(music, "load_stations",
                        lambda path=None: {"Groove Salad": "https://x"})
    out = cast_mod.play("polka hour", "kitchen")
    assert out["ok"] is False
    assert out["stations"] == ["Groove Salad"]


def test_play_ambiguous_speaker_lists_speakers(fake_house, monkeypatch):
    from prana.voice import music
    monkeypatch.delenv(cast_mod.DEFAULT_ENV, raising=False)
    monkeypatch.setattr(music, "load_stations",
                        lambda path=None: {"Groove Salad": "https://x"})
    out = cast_mod.play("groove", "")
    assert out["ok"] is False
    assert out["speakers"] == ["Kitchen speaker", "Bedroom"]


def test_stop_with_no_target_stops_everything_started(fake_house,
                                                      monkeypatch):
    from prana.voice import music
    monkeypatch.setattr(music, "load_stations",
                        lambda path=None: {"Groove Salad": "https://x"})
    cast_mod.play("groove", "kitchen")
    cast_mod.play("groove", "bedroom")
    out = cast_mod.stop()
    assert out["ok"] is True
    assert out["stopped"] == ["Bedroom", "Kitchen speaker"]
    assert all(c.quit for c in fake_house)


def test_stop_with_nothing_active_says_so(fake_house):
    out = cast_mod.stop()
    assert out["ok"] is False
