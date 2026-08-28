"""B5 — the audio-owner state machine. One track ever; sessions always
win; a restart never resumes by itself."""

from __future__ import annotations

import asyncio
from pathlib import Path

from prana.voice import music
from prana.voice.music import IDLE, PLAYING, SESSION, MusicPlayer


def make_player(tmp_path: Path, start_ok: bool = True) -> MusicPlayer:
    stations = tmp_path / "stations.yaml"
    stations.write_text(
        "Groove Salad: https://example.test/gs\n"
        "Drone Zone: https://example.test/dz\n", encoding="utf-8")
    p = MusicPlayer(room=None, stations_path=stations)
    p.pipeline_log = []

    async def fake_start(name, url):
        p.pipeline_log.append(("start", name))
        if not start_ok:
            p.last_error = "boom"
        return start_ok

    async def fake_stop():
        p.pipeline_log.append(("stop",))

    p._start_pipeline = fake_start
    p._stop_pipeline = fake_stop
    return p


def run(coro):
    return asyncio.run(coro)


def test_resolve_station_matching(tmp_path: Path):
    stations = {"Groove Salad": "u", "Drone Zone": "u2"}
    assert music.resolve_station("groove salad", stations) == "Groove Salad"
    assert music.resolve_station("drone", stations) == "Drone Zone"
    assert music.resolve_station("jazz", stations) is None
    assert music.resolve_station("", stations) is None


def test_load_stations_rejects_non_http(tmp_path: Path):
    f = tmp_path / "s.yaml"
    f.write_text("ok: https://a.test/x\nbad: file:///etc/passwd\n"
                 "worse: 42\n", encoding="utf-8")
    s = music.load_stations(f)
    assert set(s) == {"ok"}


def test_play_then_session_pauses_then_resumes(tmp_path: Path):
    async def flow():
        p = make_player(tmp_path)
        r = await p.play("groove")
        assert r["playing"] and p.state == PLAYING
        assert p.is_playing

        await p.pause_for_session()
        assert p.state == SESSION and not p.is_playing
        assert p.current is None          # nothing publishing
        assert p.pending == "Groove Salad"

        await p.resume_after_session()
        assert p.state == PLAYING
        assert p.current == "Groove Salad"
    run(flow())


def test_play_during_session_queues(tmp_path: Path):
    async def flow():
        p = make_player(tmp_path)
        await p.pause_for_session()       # session opened with no music
        r = await p.play("drone")
        assert not r["playing"] and r["queued"] == "Drone Zone"
        assert p.state == SESSION         # session still owns audio
        await p.resume_after_session()
        assert p.state == PLAYING and p.current == "Drone Zone"
    run(flow())


def test_stop_clears_pending_even_in_session(tmp_path: Path):
    async def flow():
        p = make_player(tmp_path)
        await p.play("groove")
        await p.pause_for_session()
        await p.stop()
        assert p.pending is None
        await p.resume_after_session()
        assert p.state == IDLE            # nothing comes back
    run(flow())


def test_fresh_player_is_idle_no_auto_resume(tmp_path: Path):
    p = make_player(tmp_path)
    assert p.state == IDLE and p.pending is None and not p.is_playing


def test_failed_stream_reports_honestly(tmp_path: Path):
    async def flow():
        p = make_player(tmp_path, start_ok=False)
        r = await p.play("groove")
        assert not r["playing"] and "boom" in r["reason"]
        assert p.state == IDLE
    run(flow())


def test_volume_clamped(tmp_path: Path):
    p = make_player(tmp_path)
    assert p.set_volume(250)["volume"] == 100
    assert p.set_volume(-5)["volume"] == 0
