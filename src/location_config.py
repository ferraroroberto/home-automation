"""Home-location config loader for the weather readout.

Holds the latitude/longitude (and an optional human label) used by the
``GET /api/weather`` endpoint to query Open-Meteo. Kept out of
``webapp_config.py`` because the location is *private data* (the repo is
public) whereas the webapp config is operational settings — different
sensitivity, different gitignore line.

The real ``config/location.json`` is gitignored (it carries the home's
coordinates); ``config/location.sample.json`` is committed as the template
with placeholder values. A missing file is **not** an error — it just means
weather is "not configured", which the endpoint surfaces with a clear shape
(HTTP 200, ``configured=False``) rather than a 500.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("melcloud.location_config")

DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "location.json"
)


@dataclass
class LocationConfig:
    """User-authored home location for the weather readout."""

    lat: float
    lon: float
    label: str = ""


def load_location_config(path: Optional[Path] = None) -> Optional[LocationConfig]:
    """Load the location config.

    Returns ``None`` when the file is missing or malformed (not configured) —
    the caller treats that as "weather unavailable", never as an error.
    """
    target = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not target.exists():
        logger.info("📂 location config not found at %s — weather disabled", target)
        return None

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("⚠️ Could not read %s (%s) — weather disabled", target, exc)
        return None

    try:
        lat = float(raw["lat"])
        lon = float(raw["lon"])
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "⚠️ %s is missing valid lat/lon (%s) — weather disabled", target, exc
        )
        return None

    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        logger.warning("⚠️ lat/lon out of range in %s — weather disabled", target)
        return None

    return LocationConfig(lat=lat, lon=lon, label=str(raw.get("label", "")))


def save_location_config(location: LocationConfig, path: Optional[Path] = None) -> None:
    """Atomically persist the user-authored home location."""
    target = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not (-90.0 <= location.lat <= 90.0 and -180.0 <= location.lon <= 180.0):
        raise ValueError("lat/lon out of range")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(
        json.dumps(
            {"lat": location.lat, "lon": location.lon, "label": location.label},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    os.replace(tmp, target)
    logger.info("💾 Saved home location to %s", target)
