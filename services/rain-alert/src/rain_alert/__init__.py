"""rain-alert — once each morning, check today's rain chance at home and, if it
is significant, DM the owner to bring the patio chair inside. Silent otherwise.

Deliberate constraints (shared with topic-watcher):
- No LLM calls. One Open-Meteo forecast request (free, no API key) and a canned
  message.
- Delivery and home coordinates come from ``homelab_core`` so the Discord bot
  token and the home location have a single source of truth instead of being
  copy-pasted / hardcoded per service.
- Stdlib only (plus homelab-core, which is also stdlib-only).

Usage:
  rain-alert              check forecast, DM only if rain is likely
  rain-alert --dry-run    print the forecast + decision, send nothing
  rain-alert --force      send the message regardless of forecast (test)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from homelab_core import Discord, home_location

HERE = Path(__file__).resolve().parent
# Runtime state (the log) lives under state/, which the repo .gitignore excludes
# from the tracked tree.
STATE_DIR = HERE.parents[1] / "state"
LOG_FILE = STATE_DIR / "rain_alert.log"

TIMEZONE = "America/Chicago"

# Home coordinates come from Home Assistant (core.ha). If that read fails, the
# only fallback is an explicit RAIN_ALERT_LAT / RAIN_ALERT_LON env override — no
# location is hardcoded here (this is a public repo).

# "Significant chance of rain" = today's max hourly precipitation probability at
# or above this.
RAIN_PROBABILITY_THRESHOLD = 40

MESSAGE = (
    "🌧️ Rain is likely today ({prob}% chance, ~{amount:.2f} in expected). "
    "Bring the patio chair inside!"
)


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(f"{ts} {msg}\n")


def resolve_home() -> tuple[float, float] | None:
    """Home coordinates from Home Assistant, else a RAIN_ALERT_LAT/LON override.

    Returns ``None`` if HA can't be read and no override is set — the caller then
    skips silently rather than alerting for some baked-in location.
    """
    try:
        return home_location()
    except Exception as exc:  # noqa: BLE001 — never let a config read kill the alert
        lat, lon = os.environ.get("RAIN_ALERT_LAT"), os.environ.get("RAIN_ALERT_LON")
        if lat and lon:
            return float(lat), float(lon)
        log(f"home_location() failed ({exc}) and RAIN_ALERT_LAT/LON unset — skipping")
        return None


def fetch_today_forecast(lat: float, lon: float) -> tuple[int, float]:
    """Return (max precipitation probability %, precipitation sum in inches) for today."""
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
        "latitude": lat,
        "longitude": lon,
        "daily": "precipitation_probability_max,precipitation_sum",
        "timezone": TIMEZONE,
        "forecast_days": 1,
        "precipitation_unit": "inch",
    })
    with urllib.request.urlopen(url, timeout=20) as resp:
        daily = json.load(resp)["daily"]
    return (
        int(daily["precipitation_probability_max"][0] or 0),
        float(daily["precipitation_sum"][0] or 0),
    )


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    dry_run = "--dry-run" in argv
    force = "--force" in argv

    home = resolve_home()
    if home is None:
        if dry_run:
            print("no home location (HA unreadable, RAIN_ALERT_LAT/LON unset)")
        return 0
    lat, lon = home
    prob, amount = fetch_today_forecast(lat, lon)
    rainy = prob >= RAIN_PROBABILITY_THRESHOLD
    summary = (
        f"rain prob max={prob}% sum={amount:.2f}in "
        f"threshold={RAIN_PROBABILITY_THRESHOLD}% -> {'ALERT' if rainy else 'quiet'}"
    )

    if dry_run:
        print(summary)
        return 0

    log(summary + (" (forced)" if force else ""))
    if rainy or force:
        Discord.from_env().send_dm(MESSAGE.format(prob=prob, amount=amount))
        log("sent DM")
    return 0


if __name__ == "__main__":
    sys.exit(main())
