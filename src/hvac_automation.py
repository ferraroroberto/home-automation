"""Per-unit HVAC automation: dynamic setpoint rules + daily schedules.

Two UI-free concerns, persisted like :mod:`src.display_names` (atomic write
to a gitignored ``config/*.json``, missing file → empty, committed
``.sample.json``):

* **Temperature rule** — a *dynamic setpoint controller*, NOT an on/off
  thermostat. The unit's own setpoint is an unreliable black box (set it to
  27 °C in cool mode and the room overshoots to 25–26 °C), so we never trust
  it and never auto power-cycle the compressor (rapid cycling damages it).
  Instead the rule holds a desired **room** temperature and the engine nudges
  the unit's *setpoint* up/down each adjustment interval to drive the room
  toward it — a slow integral loop wrapped around the unreliable thermostat.
  Per-mode targets (``cool_target`` / ``heat_target``); the active one is
  chosen by the unit's current operation mode. Dormant when the unit is off
  or in Auto/Fan (no meaningful setpoint to steer).

* **Schedule** — a full settings profile (power, mode, setpoint, fan, vanes)
  applied once at a daily local ``HH:MM``. Orthogonal to the rule: the
  schedule sets *how* the unit runs; the rule thereafter only steers the
  setpoint while the unit is on.

The pure control decision (:func:`next_setpoint`) lives here so it is unit
-testable without an event loop or the MELCloud client; the asyncio engine
that calls ``fetch_devices`` / ``set_device_state`` is
:mod:`app.webapp.automation`.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
RULES_PATH = _CONFIG_DIR / "hvac_rules.json"
SCHEDULES_PATH = _CONFIG_DIR / "hvac_schedules.json"

# Modes the controller can steer, and the direction lowering the setpoint
# pushes the room. Cool/Dry: lower setpoint → more cooling → room falls.
# Heat: higher setpoint → more heat → room rises. Auto/Fan are not steerable.
COOL_MODES = frozenset({"Cool", "Dry"})
HEAT_MODES = frozenset({"Heat"})


@dataclass
class TempRule:
    """Dynamic-setpoint rule for one unit (per-mode desired room temps)."""

    enabled: bool = False
    cool_target: Optional[float] = None
    heat_target: Optional[float] = None


@dataclass
class Schedule:
    """Daily settings profile applied at a local ``HH:MM`` for one unit."""

    enabled: bool = False
    time: str = "08:00"  # local HH:MM, recurs daily
    power: bool = True
    operation_mode: Optional[str] = None
    set_temperature: Optional[float] = None
    fan_speed: Optional[str] = None
    vane_vertical_direction: Optional[str] = None
    vane_horizontal_direction: Optional[str] = None


# --------------------------------------------------------------- control law
def target_for_mode(rule: TempRule, operation_mode: Optional[str]) -> Optional[float]:
    """Desired room target for the unit's current mode, or ``None`` if dormant.

    Returns ``None`` when the rule is disabled, the mode is not steerable
    (Auto/Fan), or no target is set for the active direction.
    """
    if not rule.enabled or operation_mode is None:
        return None
    if operation_mode in COOL_MODES:
        return rule.cool_target
    if operation_mode in HEAT_MODES:
        return rule.heat_target
    return None


def next_setpoint(
    *,
    operation_mode: Optional[str],
    room_temperature: Optional[float],
    set_temperature: Optional[float],
    target: Optional[float],
    buffer: float,
    step: float,
    tmin: float,
    tmax: float,
) -> Optional[float]:
    """One nudge of the unit's setpoint toward the desired room ``target``.

    Returns the new setpoint, or ``None`` to hold (room within ±``buffer`` of
    target, an un-steerable mode, missing readings, or the nudge would not
    change the clamped value). The setpoint moves by one ``step`` per call so
    the slow-responding room has time to react before the next adjustment.
    """
    if room_temperature is None or set_temperature is None or target is None:
        return None

    if operation_mode in COOL_MODES:
        if room_temperature > target + buffer:
            new = set_temperature - step  # too warm → cool harder
        elif room_temperature < target - buffer:
            new = set_temperature + step  # overcooled → ease off
        else:
            return None
    elif operation_mode in HEAT_MODES:
        if room_temperature < target - buffer:
            new = set_temperature + step  # too cold → heat harder
        elif room_temperature > target + buffer:
            new = set_temperature - step  # overheated → ease off
        else:
            return None
    else:
        return None

    new = max(tmin, min(tmax, new))
    if new == set_temperature:
        return None
    return new


# --------------------------------------------------------------- persistence
def _load(path: Path) -> Dict[str, dict]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("⚠️ Could not read %s (%s); returning empty", path, exc)
        return {}
    if not isinstance(raw, dict):
        logger.warning("⚠️ %s is not a JSON object; returning empty", path)
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


def _save(path: Path, data: Dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    logger.info("💾 Saved %s", path)


def load_rules(path: Optional[Path] = None) -> Dict[str, TempRule]:
    """Return {unit_id: TempRule} from disk, or {} if absent."""
    target = Path(path) if path is not None else RULES_PATH
    return {uid: TempRule(**raw) for uid, raw in _load(target).items()}


def save_rules(rules: Dict[str, TempRule], path: Optional[Path] = None) -> None:
    """Atomically persist the whole rule map."""
    target = Path(path) if path is not None else RULES_PATH
    _save(target, {uid: asdict(r) for uid, r in rules.items()})


def set_rule(unit_id: str, rule: TempRule, path: Optional[Path] = None) -> None:
    """Set (or, when fully default+disabled, drop) one unit's rule."""
    rules = load_rules(path)
    if rule.enabled or rule.cool_target is not None or rule.heat_target is not None:
        rules[unit_id] = rule
    else:
        rules.pop(unit_id, None)
    save_rules(rules, path)


def load_schedules(path: Optional[Path] = None) -> Dict[str, Schedule]:
    """Return {unit_id: Schedule} from disk, or {} if absent."""
    target = Path(path) if path is not None else SCHEDULES_PATH
    return {uid: Schedule(**raw) for uid, raw in _load(target).items()}


def save_schedules(schedules: Dict[str, Schedule], path: Optional[Path] = None) -> None:
    """Atomically persist the whole schedule map."""
    target = Path(path) if path is not None else SCHEDULES_PATH
    _save(target, {uid: asdict(s) for uid, s in schedules.items()})


def set_schedule(unit_id: str, schedule: Schedule, path: Optional[Path] = None) -> None:
    """Set (or, when disabled with no profile, drop) one unit's schedule."""
    schedules = load_schedules(path)
    if schedule.enabled:
        schedules[unit_id] = schedule
    else:
        schedules.pop(unit_id, None)
    save_schedules(schedules, path)
