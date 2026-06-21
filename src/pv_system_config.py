"""PV-array config loader for the Energy-tab solar-forecast section (issue #39).

Holds the installed array parameters — peak power (kWp), panel tilt and azimuth,
and a derate / performance ratio — used by :mod:`src.pv_forecast` to turn
Open-Meteo's global tilted irradiance into an expected-generation curve. Kept out
of ``webapp_config.py`` for the same reason ``location_config.py`` is: this is
*user-authored system data*, not operational webapp settings.

The real ``config/pv_system.json`` is gitignored; ``config/pv_system.sample.json``
is committed as the template. A missing or malformed file is **not** an error — it
just means the forecast is "not configured", which the endpoint surfaces with a
clear shape (HTTP 200, ``available=False``) rather than a 500.

Azimuth follows Open-Meteo's convention: 0 = South, -90 = East, 90 = West,
180 = North. See ``docs/pv-forecast.md``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("melcloud.pv_system_config")

DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "pv_system.json"
)


@dataclass
class PvSystemConfig:
    """User-authored PV-array parameters for the generation forecast."""

    kwp: float
    tilt_deg: float = 30.0
    azimuth_deg: float = 0.0
    performance_ratio: float = 0.8


def load_pv_system_config(path: Optional[Path] = None) -> Optional[PvSystemConfig]:
    """Load the PV-system config.

    Returns ``None`` when the file is missing or malformed (not configured) — the
    caller treats that as "forecast unavailable", never as an error.
    """
    target = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not target.exists():
        logger.info("📂 PV-system config not found at %s — forecast disabled", target)
        return None

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("⚠️ Could not read %s (%s) — forecast disabled", target, exc)
        return None

    try:
        kwp = float(raw["kwp"])
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("⚠️ %s is missing a valid kwp (%s) — forecast disabled", target, exc)
        return None

    if kwp <= 0:
        logger.warning("⚠️ kwp must be positive in %s — forecast disabled", target)
        return None

    tilt = _clamp_float(raw.get("tilt_deg"), default=30.0, lo=0.0, hi=90.0)
    azimuth = _clamp_float(raw.get("azimuth_deg"), default=0.0, lo=-180.0, hi=180.0)
    pr = _clamp_float(raw.get("performance_ratio"), default=0.8, lo=0.0, hi=1.0)

    return PvSystemConfig(kwp=kwp, tilt_deg=tilt, azimuth_deg=azimuth, performance_ratio=pr)


def _clamp_float(value: object, default: float, lo: float, hi: float) -> float:
    """Coerce ``value`` to a float clamped to ``[lo, hi]``, falling back to ``default``."""
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, out))
