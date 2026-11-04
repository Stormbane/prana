"""Open-Meteo weather fetcher for the body's home.

The home's place comes from prana.site (the private site.json), never from
this public repo; with no place configured there is no fetch. Open-Meteo is
a free, no-auth weather API that serves current and forecast data directly
from public weather model output. Polled by cognition every ~10 minutes
and pushed to the body via ExpressionClient.set_weather().
"""

from __future__ import annotations

import json
import logging
from urllib.error import URLError
from urllib.request import urlopen

from deha.expression.layers import Weather
from prana.site import Site, load_site

logger = logging.getLogger(__name__)

OPEN_METEO = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lon}"
    "&current=temperature_2m,precipitation,weather_code,cloud_cover,"
    "wind_speed_10m,wind_direction_10m"
    "&timezone=Australia%2FBrisbane"
)

DEFAULT_TIMEOUT = 10.0


def open_meteo_url(site: Site | None = None) -> str | None:
    """The forecast URL for the home's place, or None when none is configured."""
    home = (site or load_site()).home
    return OPEN_METEO.format(lat=home[0], lon=home[1]) if home else None


def fetch_home_weather(timeout: float = DEFAULT_TIMEOUT, site: Site | None = None) -> Weather | None:
    """Fetch current conditions at the home's place from Open-Meteo.

    Returns a Weather instance, or None if the fetch failed (network down,
    API hiccup) or no place is configured. Cognition should treat None as
    "keep last known weather" and try again next cycle.
    """
    url = open_meteo_url(site)
    if url is None:
        logger.info("No home place in the site file; no weather fetch.")
        return None
    try:
        with urlopen(url, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (URLError, TimeoutError, json.JSONDecodeError) as e:
        logger.warning("Weather fetch failed: %s: %s", type(e).__name__, e)
        return None

    try:
        cur = payload["current"]
        return Weather(
            wind_speed_kmh=float(cur.get("wind_speed_10m", 0.0)),
            wind_direction_deg=float(cur.get("wind_direction_10m", 0.0)),
            precipitation_mm_hr=float(cur.get("precipitation", 0.0)),
            cloud_cover_pct=float(cur.get("cloud_cover", 0.0)),
            weather_code=int(cur.get("weather_code", 0)),
            temperature_c=float(cur.get("temperature_2m", 22.0)),
        ).clamped()
    except (KeyError, TypeError, ValueError) as e:
        logger.warning("Weather payload parse failed: %s: %s", type(e).__name__, e)
        return None
