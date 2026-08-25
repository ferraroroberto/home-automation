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

The optional ``thermal_model_enabled`` switch (issue #591) selects which of two
meanings ``performance_ratio`` carries, so the two can never be mixed up:

* **off** (the default, and what every existing file gets) — no temperature term
  in the model, so ``performance_ratio`` is the *combined* derate with a constant
  thermal allowance folded in (~0.80 for this home's array).
* **on** — :mod:`src.pv_forecast` applies a PVWatts-style cell-temperature
  derate, so ``performance_ratio`` must be a *system-loss-only* factor (~0.88).

Flipping the switch without migrating the ratio would double-count the thermal
loss, so that combination is refused rather than computed:
:func:`thermal_migration_error` names it, the strict validator raises it (a 400
from the editor) and the forecast reports it as unavailable rather than emitting
numbers that are quietly ~10% low.

The optional ``horizon_profile`` (issue #578 part b) is a hand-entered
obstruction-elevation-by-azimuth profile, editable from the PV system card in
the same staged-dialog style as the panel rows. Its azimuth is **compass,
clockwise from true north** (0/90/180/270 = N/E/S/W) — :mod:`src.sun_position`'s
convention, deliberately *not* ``arrays[].azimuth_deg``'s Open-Meteo
south-relative one, because the profile is compared against a computed sun
azimuth, never a panel orientation. Like the panel-temperature switch,
``horizon_profile_enabled`` has no editor control and defaults to **off** — the
owner arms it by hand once the geometry has been entered. The list of points is
editable (and saved) regardless of the switch, so the profile can be built
before it is armed.

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

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from src._atomic_json import write_json_atomic
from src._schedule_store import read_json

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
class PvHorizonPoint:
    """One obstruction-elevation sample of the horizon/shading profile (issue
    #578 part b). ``azimuth_deg`` is compass, clockwise from true north — see
    the module docstring; ``elevation_deg`` is how high the obstruction stands
    above the horizontal at that azimuth."""

    azimuth_deg: float
    elevation_deg: float = 0.0


@dataclass
class PvSystemConfig:
    """User-authored PV-array parameters for the generation forecast."""

    arrays: List[PvArray] = field(default_factory=list)
    performance_ratio: float = 0.8
    # Issue #591 — off by default so the live forecast is unchanged. See the
    # module docstring for what turning it on also requires.
    thermal_model_enabled: bool = False
    # Issue #578 part (b) — off by default; see the module docstring. The
    # points themselves are editable independently of the switch.
    horizon_profile_enabled: bool = False
    horizon_profile: List[PvHorizonPoint] = field(default_factory=list)

    @property
    def total_kwp(self) -> float:
        return sum(a.kwp for a in self.arrays)


# With the thermal term ON, ``performance_ratio`` is a system-loss-only factor
# (~0.88 measured for this array: PVGIS-style 14% system loss, nothing thermal).
# The pre-#591 combined ratios sit around 0.80 because they also absorb a
# constant ~6% thermal allowance. Anything below this floor is therefore still
# an un-migrated combined ratio, and running the temperature term on top of it
# would subtract the thermal loss twice.
MIN_THERMAL_PERFORMANCE_RATIO = 0.85


def load_pv_system_config(path: Optional[Path] = None) -> Optional[PvSystemConfig]:
    """Load the PV-system config.

    Returns ``None`` when the file is absent, or its content is malformed
    (not configured) — the caller treats that as "forecast unavailable",
    never as an error.

    Raises :class:`~src._schedule_store.StoreUnreadableError` when the file
    *exists* but can't be read (issue #692) — ``update_pv_system`` mutates
    this return value and saves it whole, so a transient failure here would
    otherwise get saved back over a real array/horizon-profile config.
    Callers for whom "not configured" is an acceptable, non-destructive
    fallback (e.g. the read-only GET endpoint) should catch it explicitly
    rather than relying on this function to fold it into ``None``.

    Accepts two shapes: the current ``{"arrays": [...], "performance_ratio": ...}``
    list form, and the legacy single-orientation flat form (``kwp``/``tilt_deg``/
    ``azimuth_deg``/``performance_ratio`` at the top level, pre-#555), which loads
    as a single-element ``arrays`` list — existing configs keep working unmigrated.

    ``thermal_model_enabled`` (issue #591) and ``horizon_profile_enabled``
    (issue #578 part b) are both absent from every existing file, and absent
    means off for both, so nothing here changes what the forecast predicts
    until one is added by hand.
    """
    target = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    raw = read_json(target, None)
    if raw is None:
        logger.info("📂 PV-system config not found at %s — forecast disabled", target)
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

    # Only a literal ``true`` arms either term. A stray "yes"/1 left in a
    # hand-edited file must not change what the card predicts by truthiness.
    thermal = raw.get("thermal_model_enabled") is True
    horizon_enabled = raw.get("horizon_profile_enabled") is True
    horizon_profile = _parse_horizon_profile(raw.get("horizon_profile"), target)

    return PvSystemConfig(
        arrays=arrays,
        performance_ratio=pr,
        thermal_model_enabled=thermal,
        horizon_profile_enabled=horizon_enabled,
        horizon_profile=horizon_profile,
    )


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


def _parse_horizon_profile(raw: object, target: Path) -> List[PvHorizonPoint]:
    """Lenient parse of the horizon/shading profile (issue #578 part b).

    A malformed point is skipped and logged, same as a malformed sub-array —
    never a reason to disable the whole forecast.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        logger.warning("⚠️ %s 'horizon_profile' must be a list — ignored", target)
        return []

    points: List[PvHorizonPoint] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            logger.warning("⚠️ %s horizon_profile[%d] is not an object — skipped", target, i)
            continue
        try:
            azimuth = float(entry["azimuth_deg"]) % 360.0
        except (KeyError, TypeError, ValueError):
            logger.warning(
                "⚠️ %s horizon_profile[%d] is missing a valid azimuth_deg — skipped",
                target, i,
            )
            continue
        elevation = _clamp_float(entry.get("elevation_deg"), default=0.0, lo=0.0, hi=90.0)
        points.append(PvHorizonPoint(azimuth_deg=azimuth, elevation_deg=elevation))
    return points


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
#
# ``thermal_model_enabled`` and ``horizon_profile_enabled`` are deliberately
# NOT owned (issues #591, #578 part b): the editor has no control for either
# switch, so a save must leave a hand-set switch exactly as it found it rather
# than silently writing the default back over it. ``horizon_profile`` itself
# *is* owned — the editor does have a control for the points.
_OWNED_KEYS = frozenset(
    {"arrays", "performance_ratio", "kwp", "tilt_deg", "azimuth_deg", "horizon_profile"}
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


def thermal_migration_error(config: PvSystemConfig) -> Optional[str]:
    """Name the one combination that would double-count the thermal loss.

    Returns ``None`` when the config is self-consistent, or a human-readable
    explanation when the temperature term is armed on top of a
    ``performance_ratio`` that still folds a constant thermal allowance in.
    Both halves of the codebase route through this one function — the strict
    writer raises it, the forecast refuses to compute on it — so the inconsistent
    state cannot silently produce numbers (issue #591).
    """
    if not config.thermal_model_enabled:
        return None
    try:
        pr = float(config.performance_ratio)
    except (TypeError, ValueError):
        return None  # the plain performance_ratio check will speak for itself
    if pr >= MIN_THERMAL_PERFORMANCE_RATIO:
        return None
    return (
        f"performance_ratio {pr:.2f} is too low for thermal_model_enabled: with "
        "the panel-temperature term on, performance_ratio must be a system-loss-"
        f"only factor (at least {MIN_THERMAL_PERFORMANCE_RATIO:.2f}, ~0.88 for a "
        "PVGIS 14% loss). A ratio around 0.80 still folds the thermal allowance "
        "in, so the loss would be counted twice — migrate the ratio in the same "
        "edit as the switch, or leave the switch off"
    )


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

    mismatch = thermal_migration_error(config)
    if mismatch is not None:
        raise ValueError(mismatch)

    for i, point in enumerate(config.horizon_profile):
        azimuth = _require_finite(point.azimuth_deg, f"horizon_profile[{i}].azimuth_deg")
        if not 0.0 <= azimuth < 360.0:
            raise ValueError(
                f"horizon_profile[{i}].azimuth_deg must be between 0 and 360 "
                "(compass, clockwise from true north)"
            )
        elevation = _require_finite(point.elevation_deg, f"horizon_profile[{i}].elevation_deg")
        if not 0.0 <= elevation <= 90.0:
            raise ValueError(f"horizon_profile[{i}].elevation_deg must be between 0 and 90")


def save_pv_system_config(
    config: PvSystemConfig, path: Optional[Path] = None
) -> None:
    """Validate and atomically persist the user-authored PV-array parameters.

    Always writes the current ``arrays`` shape, and always preserves any
    top-level key this module doesn't own — an existing file's ``_doc`` note
    is why the write merges instead of replacing. Raises
    :class:`~src._schedule_store.StoreUnreadableError` (issue #692) when an
    existing file can't be read for that merge — writing anyway would drop
    the unowned keys (notably ``thermal_model_enabled``/
    ``horizon_profile_enabled``) it exists to preserve.
    """
    validate_pv_system(config)
    target = Path(path) if path is not None else DEFAULT_CONFIG_PATH

    payload: Dict[str, Any] = {}
    existing = read_json(target, None)
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
    payload["horizon_profile"] = [
        {"azimuth_deg": float(p.azimuth_deg), "elevation_deg": float(p.elevation_deg)}
        for p in config.horizon_profile
    ]

    write_json_atomic(target, payload)
    logger.info(
        "💾 Saved PV system (%d sub-array(s), %.2f kWp) to %s",
        len(config.arrays),
        config.total_kwp,
        target,
    )
