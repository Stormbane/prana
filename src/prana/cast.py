"""Google Cast — Narada's radio on the house speakers.

The speakers are their own audio path: casting never touches the
LiveKit room or the box, so the benched-music SCTP lesson does not
apply (no second participant, no second track — the speaker fetches
the stream itself and prana steps out of the audio path entirely).
A cast KEEPS PLAYING through voice sessions by design (Suti,
2026-09-07): the box mic will hear distant music; TV mode exists for
when that bites.

The capability lives here; every surface (CLI below, the voice tools)
is a thin caller — the speech engine is the organ of speech, not
another brain (Suti, 2026-09-07). Stations are the same
~/.narada/music/stations.yaml the box radio uses.

CLI:
    python -m prana.cast list [--refresh]
    python -m prana.cast play <station> [--on <speaker>]
    python -m prana.cast stop [--on <speaker>]
    python -m prana.cast vol <0-100> [--on <speaker>]
    python -m prana.cast status
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

DISCOVER_TIMEOUT_S = 6.0
CONNECT_TIMEOUT_S = 10.0
CACHE_TTL_S = 300.0

# Speaker to use when none is named — set NARADA_CAST_DEFAULT to a
# friendly name (a Google-Home speaker GROUP like "everywhere" works
# and is the natural default for "play music").
DEFAULT_ENV = "NARADA_CAST_DEFAULT"

_cache: dict = {"at": 0.0, "casts": [], "browser": None}
_active: set[str] = set()  # friendly names Narada started casts on


def _content_type(url: str) -> str:
    path = url.split("?", 1)[0].lower()
    if path.endswith((".m3u8", ".m3u")):
        return "application/x-mpegurl"
    if path.endswith(".aac"):
        return "audio/aac"
    if path.endswith((".ogg", ".oga")):
        return "audio/ogg"
    return "audio/mpeg"


def resolve_speaker(query: str, names: list[str]) -> Optional[str]:
    """Exact then substring match, case-insensitive — the same manners
    as music.resolve_station. Empty query: the NARADA_CAST_DEFAULT
    speaker if it exists, else the only speaker if there is only one."""
    q = (query or "").strip().lower()
    if not q:
        default = os.environ.get(DEFAULT_ENV, "").strip().lower()
        if default:
            return resolve_speaker(default, names)
        return names[0] if len(names) == 1 else None
    for n in names:
        if n.lower() == q:
            return n
    for n in names:
        if q in n.lower():
            return n
    return None


def _discover(refresh: bool = False) -> list:
    """Chromecast objects on the LAN, cached for CACHE_TTL_S — mDNS
    discovery costs seconds and voice tools shouldn't pay it twice."""
    import pychromecast

    now = time.monotonic()
    if (not refresh and _cache["casts"]
            and now - _cache["at"] < CACHE_TTL_S):
        return _cache["casts"]
    old = _cache["browser"]
    casts, browser = pychromecast.get_chromecasts(
        timeout=DISCOVER_TIMEOUT_S)
    # The cast objects resolve their host THROUGH this browser's
    # zeroconf when they connect — stopping it here bricks every later
    # wait() (field 2026-09-07: connect timed out against a speaker
    # that list had just found). Keep it alive with the cache; retire
    # only the browser it replaces.
    casts = sorted(casts, key=lambda c: c.name or "")
    _cache.update(at=now, casts=casts, browser=browser)
    if old is not None:
        try:
            old.stop_discovery()
        except Exception:
            pass
    return casts


def _find(speaker_query: str, refresh: bool = False):
    """(cast, error_dict) — exactly one is None."""
    casts = _discover(refresh)
    names = [c.name for c in casts]
    if not casts:
        return None, {"ok": False,
                      "reason": "no cast speakers found on the network"}
    name = resolve_speaker(speaker_query, names)
    if name is None:
        return None, {"ok": False,
                      "reason": ("which speaker?" if not speaker_query
                                 else f"no speaker matches '{speaker_query}'"),
                      "speakers": names}
    return next(c for c in casts if c.name == name), None


def speakers(refresh: bool = False) -> list[dict]:
    return [{"name": c.name,
             "model": c.model_name,
             "is_group": c.cast_type == "group"}
            for c in _discover(refresh)]


def play(station_query: str, speaker_query: str = "") -> dict:
    """Start a station on a speaker. Blocking (seconds) — callers on an
    event loop wrap this in a thread."""
    from prana.voice.music import load_stations, resolve_station

    stations = load_stations()
    if not stations:
        return {"ok": False,
                "reason": "no stations configured "
                          "(~/.narada/music/stations.yaml)"}
    station = resolve_station(station_query, stations)
    if station is None:
        return {"ok": False, "reason": "no such station",
                "stations": sorted(stations)[:12]}

    cast, err = _find(speaker_query)
    if err is not None:
        return err
    url = stations[station]
    try:
        cast.wait(timeout=CONNECT_TIMEOUT_S)
        mc = cast.media_controller
        mc.play_media(url, _content_type(url),
                      title=f"Narada radio — {station}",
                      stream_type="LIVE")
        mc.block_until_active(timeout=CONNECT_TIMEOUT_S)
    except Exception as exc:
        # One stale-cache retry: speakers reboot and change addresses.
        logger.info("cast play failed (%s) — rediscovering", exc)
        cast, err = _find(speaker_query, refresh=True)
        if err is not None:
            return err
        try:
            cast.wait(timeout=CONNECT_TIMEOUT_S)
            mc = cast.media_controller
            mc.play_media(url, _content_type(url),
                          title=f"Narada radio — {station}",
                          stream_type="LIVE")
            mc.block_until_active(timeout=CONNECT_TIMEOUT_S)
        except Exception as exc2:
            return {"ok": False,
                    "reason": f"speaker '{cast.name}' unreachable: {exc2}"}
    _active.add(cast.name)
    return {"ok": True, "station": station, "speaker": cast.name}


def stop(speaker_query: str = "") -> dict:
    """Stop the cast. No speaker named = every speaker Narada started
    a cast on this process — 'stop the music' means all of it."""
    targets: list[str]
    if speaker_query:
        cast, err = _find(speaker_query)
        if err is not None:
            return err
        targets = [cast.name]
    elif _active:
        targets = sorted(_active)
    else:
        return {"ok": False, "reason": "nothing casting (that I started)"}

    stopped, failed = [], []
    for name in targets:
        cast, err = _find(name)
        if err is not None:
            failed.append(name)
            continue
        try:
            cast.wait(timeout=CONNECT_TIMEOUT_S)
            cast.quit_app()
            stopped.append(name)
        except Exception as exc:
            logger.warning("cast stop failed on %s: %s", name, exc)
            failed.append(name)
        _active.discard(name)
    out: dict = {"ok": bool(stopped), "stopped": stopped}
    if failed:
        out["failed"] = failed
    return out


def volume(percent: int, speaker_query: str = "") -> dict:
    cast, err = _find(speaker_query)
    if err is not None:
        return err
    pct = max(0, min(100, int(percent)))
    try:
        cast.wait(timeout=CONNECT_TIMEOUT_S)
        cast.set_volume(pct / 100.0)
    except Exception as exc:
        return {"ok": False,
                "reason": f"speaker '{cast.name}' unreachable: {exc}"}
    return {"ok": True, "speaker": cast.name, "volume": pct}


def status(refresh: bool = False) -> list[dict]:
    """What each speaker is doing. Connects to every speaker — CLI
    patience, not voice-tool patience."""
    out = []
    for cast in _discover(refresh):
        row = {"speaker": cast.name}
        try:
            cast.wait(timeout=CONNECT_TIMEOUT_S)
            row["app"] = cast.app_display_name
            st = cast.media_controller.status
            if st and st.player_state:
                row["state"] = st.player_state
                if st.title:
                    row["title"] = st.title
            row["volume"] = int(round((cast.status.volume_level or 0) * 100))
        except Exception as exc:
            row["error"] = str(exc)
        out.append(row)
    return out


# ── CLI ──────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    import json

    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser(prog="prana.cast",
                                 description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", help="discover speakers")
    p.add_argument("--refresh", action="store_true")
    p = sub.add_parser("play", help="cast a station")
    p.add_argument("station")
    p.add_argument("--on", default="", help="speaker or group name")
    p = sub.add_parser("stop", help="stop casting")
    p.add_argument("--on", default="")
    p = sub.add_parser("vol", help="set speaker volume")
    p.add_argument("percent", type=int)
    p.add_argument("--on", default="")
    p = sub.add_parser("status", help="what every speaker is doing")
    p.add_argument("--refresh", action="store_true")
    args = ap.parse_args(argv)

    if args.cmd == "list":
        result: object = speakers(refresh=args.refresh)
    elif args.cmd == "play":
        result = play(args.station, args.on)
    elif args.cmd == "stop":
        result = stop(args.on)
    elif args.cmd == "vol":
        result = volume(args.percent, args.on)
    else:
        result = status(refresh=args.refresh)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    ok = result if isinstance(result, list) else [result]
    return 0 if all(r.get("ok", True) for r in ok if isinstance(r, dict)) \
        else 1


if __name__ == "__main__":
    raise SystemExit(main())
