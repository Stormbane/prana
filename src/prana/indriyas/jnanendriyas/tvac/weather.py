"""Open-Meteo weather fetcher for the body's physical location.

The body lives in Kallangur, QLD (~27.293 S, 153.020 E). Open-Meteo is a
free, no-auth weather API that serves current and forecast data directly
from public weather model output. Polled by cognition every ~10 minutes
and pushed to the body via ExpressionClient.set_weather().
"""

from __future__ import annotations

import json
import logging
from urllib.error import URLError
from urllib.request import urlopen

from deha.expression.layers import Weather

logger = logging.getLogger(__name__)

# Kallangur, QLD coordinates. The body's physical location.
KALLANGUR_LAT = -27.293
KALLANGUR_LON = 153.020

OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    f"?latitude={KALLANGUR_LAT}&longitude={KALLANGUR_LON}"
    "&current=temperature_2m,precipitation,weather_code,cloud_cover,"
    "wind_speed_10m,wind_direction_10m"
    "&timezone=Australia%2FBrisbane"
)

DEFAULT_TIMEOUT = 10.0


def fetch_kallangur_weather(timeout: float = DEFAULT_TIMEOUT) -> Weather | None:
    """Fetch current conditions for Kallangur from Open-Meteo.

    Returns a Weather instance, or None if the fetch failed (network down,
    API hiccup). Cognition should treat None as "keep last known weather"
    and try again next cycle.
    """
    try:
        with urlopen(OPEN_METEO_URL, timeout=timeout) as resp:
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
