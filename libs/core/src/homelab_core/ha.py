"""Read the configured home location from a local Home Assistant install.

HA stores it in ``config/.storage/core.config``, which is root-owned, so this
needs passwordless sudo on the host. Location-based services (e.g. rain-alert)
use this so the home coordinates have a single source of truth instead of being
hardcoded per service.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

DEFAULT_CORE_CONFIG = "~/services/homeassistant/config/.storage/core.config"


def home_location(core_config: str = DEFAULT_CORE_CONFIG) -> tuple[float, float]:
    """Return ``(latitude, longitude)`` from Home Assistant's stored config."""
    path = str(Path(core_config).expanduser())
    out = subprocess.run(
        ["sudo", "-n", "cat", path], check=False, text=True, capture_output=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"could not read HA config via sudo: {out.stderr.strip()}")
    data = json.loads(out.stdout)["data"]
    return float(data["latitude"]), float(data["longitude"])
