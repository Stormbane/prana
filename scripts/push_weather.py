"""Fetch current weather for Kallangur and push it to the body.

Run as a one-shot or on a cron (~every 10 minutes). The body holds the
last value indefinitely, so missing a cycle is fine — the only cost is
that visual conditions on the screen lag the world for a bit.

Usage:
    python scripts/push_weather.py
    python scripts/push_weather.py --device 192.168.86.35
"""

from __future__ import annotations

import argparse
import logging
import sys

from prana.indriyas.karmendriyas.drishti.expression import ExpressionClient, DEFAULT_DEVICE_IP
from prana.indriyas.jnanendriyas.tvac.weather import fetch_kallangur_weather

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("push_weather")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--device", default=DEFAULT_DEVICE_IP)
    args = parser.parse_args()

    weather = fetch_kallangur_weather()
    if weather is None:
        log.error("Weather fetch returned no data; nothing to push.")
        return 1

    log.info(
        "Kallangur now: %.1f°C, wind %.1f km/h @ %.0f°, precip %.2f mm/h, "
        "cloud %.0f%%, code %d",
        weather.temperature_c,
        weather.wind_speed_kmh,
        weather.wind_direction_deg,
        weather.precipitation_mm_hr,
        weather.cloud_cover_pct,
        weather.weather_code,
    )

    client = ExpressionClient(device_ip=args.device)
    if client.set_weather(weather):
        log.info("Pushed to body at %s.", args.device)
        return 0
    log.error("Push to body failed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
