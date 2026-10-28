"""rain-notifier — once each morning, check today's rain chance at home and, if it
is significant, DM the owner to bring the patio chair inside. Silent otherwise.

Deliberate constraints:
- No LLM calls. One Open-Meteo forecast request (free, no API key) and a canned
  message.
- Delivery and home coordinates come from ``homelab_core`` so the Discord bot
  token and the home location have a single source of truth instead of being
  copy-pasted / hardcoded per service.
- Stdlib only (plus homelab-core, which is also stdlib-only).

Usage:
  rain-notifier              check forecast, DM only if rain is likely
  rain-notifier --dry-run    print the forecast + decision, send nothing
  rain-notifier --force      send the message regardless of forecast (test)
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from homelab_core import Discord, home_location

HERE = Path(__file__).resolve().parent
# Runtime state (the log) lives under state/, which the repo .gitignore excludes
# from the tracked tree.
STATE_DIR = HERE.parents[1] / "state"
LOG_FILE = STATE_DIR / "rain_notifier.log"

TIMEZONE = "America/Chicago"

# Home coordinates come from Home Assistant (core.ha). If that read fails, the
# only fallback is an explicit RAIN_NOTIFIER_LAT / RAIN_NOTIFIER_LON env override — no
# location is hardcoded here (this is a public repo).

# "Significant chance of rain" = today's max hourly precipitation probability at
# or above this.
RAIN_PROBABILITY_THRESHOLD = 40

# Open-Meteo occasionally returns a transient 5xx or drops the connection. A
# once-a-day job must not go hard-down over a momentary blip, so retry a few
# times with exponential backoff before giving up.
FETCH_RETRIES = 3

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
    """Home coordinates from Home Assistant, else a RAIN_NOTIFIER_LAT/LON override.

    Returns ``None`` if HA can't be read and no override is set — the caller then
    skips silently rather than alerting for some baked-in location.
    """
    try:
        return home_location()
    except Exception as exc:  # noqa: BLE001 — never let a config read kill the alert
        lat, lon = os.environ.get("RAIN_NOTIFIER_LAT"), os.environ.get("RAIN_NOTIFIER_LON")
        if lat and lon:
            return float(lat), float(lon)
        log(f"home_location() failed ({exc}) and RAIN_NOTIFIER_LAT/LON unset — skipping")
        return None


def fetch_today_forecast(lat: float, lon: float) -> tuple[int, float]:
    """Return (max precipitation probability %, precipitation sum in inches) for today.

    Retries transient failures (``urllib`` raises ``URLError`` / ``HTTPError`` /
    socket timeouts, all ``OSError`` subclasses) with exponential backoff, and
    re-raises the last error if every attempt fails so the caller can decide how
    to degrade.
    """
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
        "latitude": lat,
        "longitude": lon,
        "daily": "precipitation_probability_max,precipitation_sum",
        "timezone": TIMEZONE,
        "forecast_days": 1,
        "precipitation_unit": "inch",
    })
    last_exc: OSError | None = None
    for attempt in range(FETCH_RETRIES):
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                daily = json.load(resp)["daily"]
            return (
                int(daily["precipitation_probability_max"][0] or 0),
                float(daily["precipitation_sum"][0] or 0),
            )
        except OSError as exc:  # URLError/HTTPError/timeout all subclass OSError
            last_exc = exc
            if attempt < FETCH_RETRIES - 1:
                time.sleep(2**attempt)  # 1s, 2s, 4s
    assert last_exc is not None  # the loop body ran at least once
    raise last_exc


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    dry_run = "--dry-run" in argv
    force = "--force" in argv

    home = resolve_home()
    if home is None:
        if dry_run:
            print("no home location (HA unreadable, RAIN_NOTIFIER_LAT/LON unset)")
        return 0
    lat, lon = home
    try:
        prob, amount = fetch_today_forecast(lat, lon)
    except OSError as exc:
        # Transient upstream outage (e.g. Open-Meteo 503) survived every retry.
        # Degrade silently like a missing location instead of failing the unit —
        # the timer will try again tomorrow.
        log(f"forecast fetch failed after {FETCH_RETRIES} tries ({exc}) — skipping")
        if dry_run:
            print(f"forecast fetch failed after {FETCH_RETRIES} tries: {exc}")
        return 0
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
