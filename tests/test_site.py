"""prana.site: private facts (the tailnet name, the home's place, the body's
LAN address) come from ~/.narada/host/site.json or the environment, never
from this public repo, and their absence degrades instead of guessing."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from prana.site import Site, load_site

REPO = Path(__file__).resolve().parents[1]


def test_values_come_from_the_file(tmp_path):
    f = tmp_path / "site.json"
    f.write_text(json.dumps({"tailnet_host": "box.example.ts.net",
                             "home": {"lat": -1.5, "lon": 2.25}, "box_ip": "10.0.0.9"}))
    s = load_site(f, env={})
    assert s == Site("box.example.ts.net", -1.5, 2.25, "10.0.0.9")
    assert s.tailnet_url(8443) == "https://box.example.ts.net:8443"
    assert s.home == (-1.5, 2.25)


def test_the_environment_overrides_the_file(tmp_path):
    f = tmp_path / "site.json"
    f.write_text(json.dumps({"tailnet_host": "a.example.ts.net", "home": {"lat": 1, "lon": 2}}))
    s = load_site(f, env={"NARADA_TAILNET_HOST": "b.example.ts.net", "NARADA_HOME_LAT": "3"})
    assert s.tailnet_host == "b.example.ts.net" and s.home == (3.0, 2.0)


def test_missing_or_broken_means_not_configured(tmp_path):
    for content in (None, "{not json", "[]"):
        f = tmp_path / "site.json"
        if content is not None:
            f.write_text(content)
        s = load_site(f, env={})
        assert s == Site() and s.tailnet_url() is None and s.home is None


def test_no_place_means_no_weather_fetch(monkeypatch):
    from prana.indriyas.jnanendriyas.tvac import weather

    def boom(*a, **k):
        raise AssertionError("no place configured: must not fetch")
    monkeypatch.setattr(weather, "urlopen", boom)
    assert weather.fetch_home_weather(site=Site()) is None
    assert weather.open_meteo_url(Site(home_lat=-1.5, home_lon=2.25)).startswith(
        "https://api.open-meteo.com/v1/forecast?latitude=-1.5&longitude=2.25")


def test_no_tailnet_name_means_no_talk_link_and_an_unconfigured_doctor_check(monkeypatch, tmp_path):
    monkeypatch.setenv("NARADA_SITE_FILE", str(tmp_path / "absent.json"))
    monkeypatch.delenv("AKHADA_TALK_URL", raising=False)
    monkeypatch.delenv("NARADA_DOCTOR_TAILNET", raising=False)
    monkeypatch.delenv("NARADA_TAILNET_HOST", raising=False)
    import importlib.util
    spec = importlib.util.spec_from_file_location("tick", REPO / "scripts" / "akhada_checkin_tick.py")
    tick = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tick)
    assert tick._talk_url() is None
    monkeypatch.setenv("NARADA_TAILNET_HOST", "box.example.ts.net")
    assert tick._talk_url() == "https://box.example.ts.net:8799/talk"
    from prana.doctor.probes import Probes
    monkeypatch.delenv("NARADA_TAILNET_HOST")
    assert Probes().tailnet_base == ""


# ── the leak guard ─────────────────────────────────────────────────────────

def _tracked_text() -> list[tuple[str, str]]:
    names = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True,
                           check=True).stdout.splitlines()
    out = []
    for name in names:
        p = REPO / name
        if p.suffix.lower() in {".png", ".jpg", ".bin", ".wav", ".onnx", ".pcm", ".ico", ".webp"}:
            continue
        try:
            out.append((name, p.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            continue
    return out


def test_no_real_tailnet_name_in_the_repo():
    """A real tailnet name looks like <machine>.tail<hex>.ts.net."""
    pattern = re.compile(r"\b[a-z0-9-]+\.tail[0-9a-f]{4,}\.ts\.net\b")
    hits = [n for n, text in _tracked_text() if pattern.search(text)]
    assert hits == []


def test_none_of_this_install_s_private_values_appear_in_the_repo():
    """Reads the real site.json (when this machine has one) and checks that
    none of its values are in a tracked file. The test itself names none."""
    from prana.site import site_file
    path = site_file()
    if not path.exists():
        pytest.skip("no site.json on this machine")
    s = load_site(path, env={})
    needles = [v for v in (s.tailnet_host, s.box_ip,
                           s.tailnet_host.split(".")[0] if s.tailnet_host else None)
               if v and len(v) >= 6]
    if s.home:
        needles += [f"{s.home[0]:.3f}", f"{s.home[1]:.3f}"]
    hits = sorted({n for n, text in _tracked_text() for v in needles if v in text})
    assert hits == [], "private values found in tracked files (names withheld)"
