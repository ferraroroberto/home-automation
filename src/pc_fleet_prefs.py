"""Preferences for the UPS-triggered PC-fleet shutdown orchestration (#498).

Unlike the all-bool toggle sets handled by :mod:`src._toggle_prefs`, this
config carries an int and a list, so it keeps its own load/save on top of
:mod:`src._atomic_json` (same pattern as :mod:`src.display_names`):

- ``enabled``           — master switch. Off means **no automatic shutdowns at
  all** (satellites *and* tower — "stay up until the end"). Supersedes the
  former ``PowerNotifyPrefs.auto_shutdown_low_battery`` toggle: when this file
  is absent, ``enabled`` seeds once from that legacy value so an existing
  opt-out survives the migration.
- ``threshold_minutes`` — trigger when UPS runtime-remaining drops to this
  many minutes or less (same semantics as the old hardcoded 15).
- ``excluded``          — hub host ids left out of the satellite shutdown
  sweep (the tower is implicitly excluded from the sweep — it always goes
  last via the local path, never via the hub).

Persisted atomically to gitignored ``config/pc_fleet.json`` (committed
``…sample.json``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from src._atomic_json import write_json_atomic

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "pc_fleet.json"

MIN_THRESHOLD_MINUTES = 1
MAX_THRESHOLD_MINUTES = 240
DEFAULT_THRESHOLD_MINUTES = 15


@dataclass(frozen=True)
class PcFleetPrefs:
    """Fleet-shutdown behaviour. Defaults: on, 15 min, nobody excluded."""

    enabled: bool = True
    threshold_minutes: int = DEFAULT_THRESHOLD_MINUTES
    excluded: Tuple[str, ...] = ()


def _clamp_threshold(value: object) -> int:
    try:
        minutes = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD_MINUTES
    return max(MIN_THRESHOLD_MINUTES, min(MAX_THRESHOLD_MINUTES, minutes))


def _legacy_enabled_seed() -> bool:
    """One-time migration seed: the old auto-shutdown toggle, if ever saved."""
    from src.power_notify_prefs import DEFAULT_PATH as LEGACY_PATH

    try:
        raw = json.loads(Path(LEGACY_PATH).read_text(encoding="utf-8"))
        return bool(raw.get("auto_shutdown_low_battery", True))
    except (OSError, json.JSONDecodeError, AttributeError):
        return True


def load_pc_fleet_prefs(path: Optional[Path] = None) -> PcFleetPrefs:
    """Return saved prefs; when the file is absent, defaults with ``enabled``
    seeded from the legacy auto-shutdown toggle (migration, see module doc)."""
    target = Path(path) if path is not None else DEFAULT_PATH
    if not target.exists():
        return PcFleetPrefs(enabled=_legacy_enabled_seed())
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("⚠️ Could not read %s (%s); using defaults", target, exc)
        return PcFleetPrefs()
    if not isinstance(raw, dict):
        logger.warning("⚠️ %s is not a JSON object; using defaults", target)
        return PcFleetPrefs()
    excluded = raw.get("excluded", ())
    if not isinstance(excluded, (list, tuple)):
        excluded = ()
    return PcFleetPrefs(
        enabled=bool(raw.get("enabled", True)),
        threshold_minutes=_clamp_threshold(raw.get("threshold_minutes")),
        excluded=tuple(str(h) for h in excluded if h),
    )


def save_pc_fleet_prefs(prefs: PcFleetPrefs, path: Optional[Path] = None) -> None:
    """Atomically persist the prefs to disk."""
    target = Path(path) if path is not None else DEFAULT_PATH
    write_json_atomic(
        target,
        {
            "enabled": prefs.enabled,
            "threshold_minutes": _clamp_threshold(prefs.threshold_minutes),
            "excluded": list(prefs.excluded),
        },
    )
    logger.info("💾 Saved pc_fleet prefs to %s", target)
