"""Where this install lives: private facts, kept out of this public repo.

The machine's tailnet name, the home's place (for weather) and the body's
LAN address identify a person and a household, so they live in
~/.narada/host/site.json, in the memory tree (git-crypt encrypted), never in
code. Each can be overridden by an environment variable. A missing value
means "not configured": callers degrade (no weather, no Talk link, the
doctor's tailnet check reads amber). They never fall back to a hardcoded
place.

site.json:
    {"tailnet_host": "<machine>.<tailnet>.ts.net",
     "home": {"lat": <latitude>, "lon": <longitude>},
     "box_ip": "<the BOX's LAN address>"}
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


def site_file() -> Path:
    return Path(os.environ.get("NARADA_SITE_FILE") or Path.home() / ".narada" / "host" / "site.json")


@dataclass(frozen=True)
class Site:
    tailnet_host: str | None = None
    home_lat: float | None = None
    home_lon: float | None = None
    box_ip: str | None = None

    def tailnet_url(self, port: int | None = None) -> str | None:
        """https://<tailnet_host>[:port], or None when not configured."""
        if not self.tailnet_host:
            return None
        return f"https://{self.tailnet_host}" + (f":{port}" if port else "")

    @property
    def home(self) -> tuple[float, float] | None:
        if self.home_lat is None or self.home_lon is None:
            return None
        return self.home_lat, self.home_lon


def _float(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def load_site(path: Path | None = None, env: dict[str, str] | None = None) -> Site:
    """The file's values, then the environment's overrides
    (NARADA_TAILNET_HOST, NARADA_HOME_LAT, NARADA_HOME_LON, NARADA_BOX_IP)."""
    env = os.environ if env is None else env
    data: dict = {}
    try:
        raw = json.loads((path or site_file()).read_text(encoding="utf-8"))
        data = raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        pass
    home = data.get("home") if isinstance(data.get("home"), dict) else {}
    return Site(
        tailnet_host=env.get("NARADA_TAILNET_HOST") or data.get("tailnet_host") or None,
        home_lat=_float(env.get("NARADA_HOME_LAT")) if env.get("NARADA_HOME_LAT") else _float(home.get("lat")),
        home_lon=_float(env.get("NARADA_HOME_LON")) if env.get("NARADA_HOME_LON") else _float(home.get("lon")),
        box_ip=env.get("NARADA_BOX_IP") or data.get("box_ip") or None,
    )
