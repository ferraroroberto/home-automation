"""PV-array config store for the Energy-tab solar-forecast section (issue #39).

Holds the installed array parameters — one or more sub-arrays, each with its own
peak power (kWp), panel tilt and azimuth, plus a single shared derate /
performance ratio — used by :mod:`src.pv_forecast` to turn Open-Meteo's global
tilted irradiance into an expected-generation curve. Multiple sub-arrays exist
because a real roof is rarely one uniform orientation (issue #555): each gets
its own Open-Meteo request and the weighted results are summed. Kept out of
``webapp_config.py`` for the same reason ``location_config.py`` is: this is
*user-authored system data*, not operational webapp settings.

The real ``config/pv_system.json`` is gitignored; ``config/pv_system.sample.json``
is committed as the template. A missing or malformed file is **not** an error — it
just means the forecast is "not configured", which the endpoint surfaces with a
clear shape (HTTP 200, ``available=False``) rather than a 500.

Azimuth follows Open-Meteo's convention: 0 = South, -90 = East, 90 = West,
180 = North — panels are always expressed at non-negative tilt, with the
opposite-facing orientation captured by azimuth alone (e.g. a panel mounted the
opposite way from a 15°-tilt south array is ``tilt_deg: 15, azimuth_deg: 180``,
never a negative tilt). See ``docs/pv-forecast.md``.

**Read and write are deliberately different contracts** (issue #561, which made
the config editable from the Energy tab). :func:`load_pv_system_config` is
*lenient* — a hand-edited file with one malformed sub-array degrades (skip +
clamp + log) rather than failing, because a broken file must never 500 the
forecast. :func:`save_pv_system_config` is *strict* — it validates and raises,
because silently dropping or clamping a row the user just typed into the editor
is a bug, not resilience. Don't "simplify" the writer by routing it through the
reader's parsing.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from src._atomic_json import write_json_atomic

logger = logging.getLogger("melcloud.pv_system_config")

DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "pv_system.json"
)


@dataclass
class PvArray:
    """One physically-uniform sub-array (a set of panels sharing an orientation)."""

    kwp: float
    tilt_deg: float = 30.0
    azimuth_deg: float = 0.0


@dataclass
class PvSystemConfig:
    """User-authored PV-array parameters for the generation forecast."""

    arrays: List[PvArray] = field(default_factory=list)
    performance_ratio: float = 0.8

    @property
    def total_kwp(self) -> float:
        return sum(a.kwp for a in self.arrays)


def load_pv_system_config(path: Optional[Path] = None) -> Optional[PvSystemConfig]:
    """Load the PV-system config.

    Returns ``None`` when the file is missing or malformed (not configured) — the
    caller treats that as "forecast unavailable", never as an error.

    Accepts two shapes: the current ``{"arrays": [...], "performance_ratio": ...}``
    list form, and the legacy single-orientation flat form (``kwp``/``tilt_deg``/
    ``azimuth_deg``/``performance_ratio`` at the top level, pre-#555), which loads
    as a single-element ``arrays`` list — existing configs keep working unmigrated.
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

    pr = _clamp_float(raw.get("performance_ratio"), default=0.8, lo=0.0, hi=1.0)

    raw_arrays = raw.get("arrays")
    if raw_arrays is None:
        # Legacy flat shape: the whole document is one sub-array.
        raw_arrays = [raw]

    if not isinstance(raw_arrays, list):
        logger.warning("⚠️ %s 'arrays' must be a list — forecast disabled", target)
        return None

    arrays: List[PvArray] = []
    for i, entry in enumerate(raw_arrays):
        parsed = _parse_array(entry, target, i)
        if parsed is not None:
            arrays.append(parsed)

    if not arrays:
        logger.warning("⚠️ %s has no valid sub-arrays — forecast disabled", target)
        return None

    return PvSystemConfig(arrays=arrays, performance_ratio=pr)


def _parse_array(entry: object, target: Path, index: int) -> Optional[PvArray]:
    """Parse one sub-array entry, or ``None`` (skipped, logged) if malformed."""
    if not isinstance(entry, dict):
        logger.warning("⚠️ %s arrays[%d] is not an object — skipped", target, index)
        return None

    try:
        kwp = float(entry["kwp"])
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "⚠️ %s arrays[%d] is missing a valid kwp (%s) — skipped", target, index, exc
        )
        return None

    if kwp <= 0:
        logger.warning("⚠️ %s arrays[%d] kwp must be positive — skipped", target, index)
        return None

    tilt = _clamp_float(entry.get("tilt_deg"), default=30.0, lo=0.0, hi=90.0)
    azimuth = _clamp_float(entry.get("azimuth_deg"), default=0.0, lo=-180.0, hi=180.0)
    return PvArray(kwp=kwp, tilt_deg=tilt, azimuth_deg=azimuth)


def _clamp_float(value: object, default: float, lo: float, hi: float) -> float:
    """Coerce ``value`` to a float clamped to ``[lo, hi]``, falling back to ``default``."""
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, out))


# The keys this module owns on disk. Everything else in the file (notably the
# hand-written ``_doc`` note explaining a home's kWp/derate choices) belongs to
# whoever wrote it and survives a save untouched. The legacy flat keys are
# listed too so a legacy file edited in the app migrates cleanly to ``arrays``
# instead of keeping a stale, contradictory ``kwp`` beside the new list.
_OWNED_KEYS = frozenset(
    {"arrays", "performance_ratio", "kwp", "tilt_deg", "azimuth_deg"}
)


def _require_finite(value: object, field_name: str) -> float:
    """Coerce to a finite float or raise ``ValueError`` naming the field."""
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a number")
    if not math.isfinite(out):
        raise ValueError(f"{field_name} must be a number")
    return out


def validate_pv_system(config: PvSystemConfig) -> None:
    """Strictly validate a config bound for disk — raises on the first problem.

    The write-path counterpart to the lenient loader: every message names the
    offending field so the API can hand it straight back as a 400 the editor
    can show against the right input.
    """
    if not config.arrays:
        raise ValueError("at least one sub-array is required")

    for i, array in enumerate(config.arrays):
        kwp = _require_finite(array.kwp, f"arrays[{i}].kwp")
        if kwp <= 0:
            raise ValueError(f"arrays[{i}].kwp must be greater than 0")

        tilt = _require_finite(array.tilt_deg, f"arrays[{i}].tilt_deg")
        if not 0.0 <= tilt <= 90.0:
            raise ValueError(
                f"arrays[{i}].tilt_deg must be between 0 and 90 — a panel facing "
                "the other way is expressed with azimuth, not a negative tilt"
            )

        azimuth = _require_finite(array.azimuth_deg, f"arrays[{i}].azimuth_deg")
        if not -180.0 <= azimuth <= 180.0:
            raise ValueError(f"arrays[{i}].azimuth_deg must be between -180 and 180")

    pr = _require_finite(config.performance_ratio, "performance_ratio")
    if not 0.0 < pr <= 1.0:
        raise ValueError("performance_ratio must be greater than 0 and at most 1")


def save_pv_system_config(
    config: PvSystemConfig, path: Optional[Path] = None
) -> None:
    """Validate and atomically persist the user-authored PV-array parameters.

    Always writes the current ``arrays`` shape, and always preserves any
    top-level key this module doesn't own — an existing file's ``_doc`` note
    is why the write merges instead of replacing.
    """
    validate_pv_system(config)
    target = Path(path) if path is not None else DEFAULT_CONFIG_PATH

    payload: Dict[str, Any] = {}
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("⚠️ Could not read %s before saving (%s)", target, exc)
            existing = None
        if isinstance(existing, dict):
            payload = {k: v for k, v in existing.items() if k not in _OWNED_KEYS}

    payload["arrays"] = [
        {
            "kwp": float(a.kwp),
            "tilt_deg": float(a.tilt_deg),
            "azimuth_deg": float(a.azimuth_deg),
        }
        for a in config.arrays
    ]
    payload["performance_ratio"] = float(config.performance_ratio)

    write_json_atomic(target, payload)
    logger.info(
        "💾 Saved PV system (%d sub-array(s), %.2f kWp) to %s",
        len(config.arrays),
        config.total_kwp,
        target,
    )
