"""Radio on the body — one audio owner, pause for sessions (B5).

Ratified scope (2026-08-28): internet radio only. Stations live in
~/.narada/music/stations.yaml (name -> stream URL) — Suti-editable,
nothing hardcoded in the tool surface.

The audio-owner state machine (cross-review #5): IDLE -> PLAYING ->
SESSION -> (resume) PLAYING. Music is fully stopped — decoder killed,
track unpublished — for the entire admitted session, so at any moment
the worker publishes AT MOST ONE audio track. That single-track
invariant is also what the firmware's renderer assumes, and it keeps
the provider-isolation claim true: while a billed session is open there
is no music in the room for the mic to re-capture. A worker restart
lands in IDLE — music never auto-resumes after a crash (no surprise
audio).

Wake-word admission is DISABLED while music plays (tap always works —
it's a data-channel signal, not audio). This is the fail-safe side of
the numeric acceptance gates: until the lyric-soak false-accept gate
passes, playback cannot open billed sessions by itself.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

MUSIC_DIR = Path.home() / ".narada" / "music"
STATIONS_FILE = MUSIC_DIR / "stations.yaml"

SAMPLE_RATE = 48000
CHANNELS = 2
FRAME_SAMPLES = SAMPLE_RATE // 100  # 10 ms

DEFAULT_VOLUME = 60

IDLE = "idle"
PLAYING = "playing"
SESSION = "session"


def load_stations(path: Path = STATIONS_FILE) -> dict[str, str]:
    """name -> url. Missing file returns {} (tools report it honestly)."""
    try:
        import yaml
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return {str(k): str(v) for k, v in raw.items()
                if isinstance(v, str) and v.startswith(("http://", "https://"))}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("stations.yaml unreadable: %s", exc)
        return {}


def resolve_station(query: str, stations: dict[str, str]) -> Optional[str]:
    """Case-insensitive exact-then-substring match. Returns name."""
    q = (query or "").strip().lower()
    if not q:
        return None
    for name in stations:
        if name.lower() == q:
            return name
    for name in stations:
        if q in name.lower():
            return name
    return None


class MusicPlayer:
    """One instance per job. All methods are called from the job's
    event loop; the frame pump is an asyncio task."""

    def __init__(self, room, stations_path: Path = STATIONS_FILE):
        self._room = room
        self._stations_path = stations_path
        self.state = IDLE
        self.volume = DEFAULT_VOLUME
        self.current: Optional[str] = None   # station name while PLAYING
        self.pending: Optional[str] = None   # station to (re)start after session
        self.last_error: Optional[str] = None
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._pump: Optional[asyncio.Task] = None
        self._source = None
        self._publication = None

    # ── public: tool surface ─────────────────────────────────────────

    async def play(self, query: str) -> dict:
        stations = load_stations(self._stations_path)
        if not stations:
            return {"playing": False,
                    "reason": "no stations configured (~/.narada/music/stations.yaml)"}
        name = resolve_station(query, stations)
        if name is None:
            return {"playing": False, "reason": "no such station",
                    "stations": sorted(stations)[:12]}
        if self.state == SESSION:
            # We're mid-conversation: the session owns the audio. Queue
            # it; resume_after_session starts it when we stop talking.
            self.pending = name
            return {"playing": False, "queued": name,
                    "reason": "will start when the conversation ends"}
        await self._stop_pipeline()
        ok = await self._start_pipeline(name, stations[name])
        if not ok:
            return {"playing": False,
                    "reason": f"stream failed: {self.last_error}"}
        self.state = PLAYING
        self.current = name
        return {"playing": True, "station": name}

    async def stop(self) -> dict:
        self.pending = None
        was = self.current
        await self._stop_pipeline()
        if self.state == PLAYING:
            self.state = IDLE
        self.current = None
        return {"stopped": True, "was": was}

    def set_volume(self, percent: int) -> dict:
        self.volume = max(0, min(100, int(percent)))
        return {"volume": self.volume}

    def now_playing(self) -> dict:
        return {
            "state": self.state,
            "station": self.current or self.pending,
            "volume": self.volume,
            "error": self.last_error,
        }

    @property
    def is_playing(self) -> bool:
        return self.state == PLAYING

    # ── public: the session boundary (the worker calls these) ────────

    async def pause_for_session(self) -> None:
        """The admitted session takes the audio. Full stop, not duck:
        the decoder dies and the track unpublishes, so nothing of the
        room's music can reach the provider through the mic."""
        if self.state == PLAYING:
            self.pending = self.current
        await self._stop_pipeline()
        self.current = None
        self.state = SESSION

    async def resume_after_session(self) -> None:
        if self.state != SESSION:
            return
        target = self.pending
        self.state = IDLE
        if target:
            result = await self.play(target)
            if not result.get("playing"):
                logger.warning("music resume failed: %s", result)

    # ── pipeline ─────────────────────────────────────────────────────

    async def _start_pipeline(self, name: str, url: str) -> bool:
        from livekit import rtc

        try:
            self._proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-i", url, "-vn",
                "-f", "s16le", "-ar", str(SAMPLE_RATE),
                "-ac", str(CHANNELS), "-",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            self.last_error = f"ffmpeg spawn: {exc}"
            return False

        try:
            self._source = rtc.AudioSource(SAMPLE_RATE, CHANNELS)
            track = rtc.LocalAudioTrack.create_audio_track(
                "narada-music", self._source)
            # SOURCE_MICROPHONE is load-bearing (field 2026-08-31): the
            # box's SDK subscribes/renders only microphone-source audio.
            # The first music track went out as UNKNOWN and played to
            # nobody — pipeline up, room silent.
            self._publication = await asyncio.wait_for(
                self._room.local_participant.publish_track(
                    track, rtc.TrackPublishOptions(
                        source=rtc.TrackSource.SOURCE_MICROPHONE)),
                timeout=5.0)
        except Exception as exc:
            self.last_error = f"publish: {exc}"
            await self._kill_proc()
            return False

        self.last_error = None
        self._pump = asyncio.create_task(self._pump_frames(name),
                                         name="music-pump")
        logger.info("music: %s (%s)", name, url)
        return True

    async def _pump_frames(self, name: str) -> None:
        from livekit import rtc

        bytes_per_frame = FRAME_SAMPLES * CHANNELS * 2
        try:
            while True:
                data = await self._proc.stdout.readexactly(bytes_per_frame)
                if self.volume < 100:
                    samples = np.frombuffer(data, dtype=np.int16)
                    scaled = (samples.astype(np.int32) * self.volume) // 100
                    data = np.clip(scaled, -32768, 32767).astype(np.int16).tobytes()
                frame = rtc.AudioFrame(
                    data=data, sample_rate=SAMPLE_RATE,
                    num_channels=CHANNELS,
                    samples_per_channel=FRAME_SAMPLES)
                await self._source.capture_frame(frame)
        except (asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        except Exception as exc:
            self.last_error = f"stream: {type(exc).__name__}"
            logger.warning("music pump ended (%s): %s", name, exc)
        finally:
            # Stream ended on its own (not a pause/stop): report honestly
            # via state; what_is_playing shows the error.
            if self.state == PLAYING:
                self.state = IDLE
                self.current = None
                if self.last_error is None:
                    self.last_error = "stream ended"

    async def _kill_proc(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
                await self._proc.wait()
            except ProcessLookupError:
                pass
            self._proc = None

    async def _stop_pipeline(self) -> None:
        # EVERY await here is bounded (field 2026-08-31: an unbounded
        # await in this path hung pause_for_session between "admitted by
        # tap" and session open — the wake loop died and taps went
        # ignored until a worker restart). Teardown is best-effort;
        # sessions must never wait on it.
        if self._pump is not None:
            self._pump.cancel()
            try:
                await asyncio.wait_for(self._pump, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
            self._pump = None
        try:
            await asyncio.wait_for(self._kill_proc(), timeout=3.0)
        except (asyncio.TimeoutError, Exception):
            self._proc = None
        if self._publication is not None:
            try:
                await asyncio.wait_for(
                    self._room.local_participant.unpublish_track(
                        self._publication.sid),
                    timeout=5.0)
            except Exception as exc:
                logger.warning("unpublish failed: %s", exc)
            self._publication = None
        self._source = None


def write_default_stations(path: Path = STATIONS_FILE) -> None:
    """First-run helper: a small curated set, Suti-editable."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Narada's radio stations — name: stream URL. Edit freely.\n"
        "ABC Classic: https://live-radio01.mediahubaustralia.com/2FMW/mp3/\n"
        "Triple J: https://mediaserviceslive.akamaized.net/hls/live/2038308/triplejnsw/masterhq.m3u8\n"
        "Radio Paradise: https://stream.radioparadise.com/mp3-192\n"
        "Groove Salad: https://ice1.somafm.com/groovesalad-256-mp3\n"
        "Drone Zone: https://ice1.somafm.com/dronezone-256-mp3\n"
        "Fluid: https://ice1.somafm.com/fluid-128-mp3\n",
        encoding="utf-8")
