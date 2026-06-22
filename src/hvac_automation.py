"""Per-unit HVAC automation: dynamic setpoint rules + daily schedules.

Two UI-free concerns, persisted like :mod:`src.display_names` (atomic write
to a gitignored ``config/*.json``, missing file → empty, committed
``.sample.json``):

* **Temperature rule** — a *dynamic setpoint controller*, NOT an on/off
  thermostat. The unit's own setpoint is an unreliable black box (set it to
  27 °C in cool mode and the room overshoots to 25–26 °C), so we never trust
  it and never auto power-cycle the compressor (rapid cycling damages it).
  Instead the rule holds a desired **room** temperature and the engine steers
  the unit's *setpoint* each adjustment interval to drive the room toward it.
  The loop is asymmetric: while the room is still past the target it nudges one
  step at a time (a slow integral drive), but the moment the room reaches the
  target it jumps the setpoint to one degree on the satisfied side so the unit
  idles immediately rather than overshooting deep and recovering one step at a
  time. Per-mode targets (``cool_target`` / ``heat_target``); the active one is
  chosen by the unit's current operation mode. Dormant when the unit is off
  or in Auto/Fan (no meaningful setpoint to steer).

* **Schedule entries** — one or more daily local ``HH:MM`` entries per unit.
  Each entry can be a simple power-off event or a power-on/full-profile event
  (mode, setpoint, fan, vanes). Orthogonal to the rule: schedules decide
  *whether/how* the unit runs; the rule thereafter only steers the setpoint
  while the unit is on.

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
from typing import Any, Dict, List, Optional

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
class ScheduleEntry:
    """One daily schedule entry for a unit."""

    id: str = "default"
    enabled: bool = True
    time: str = "08:00"  # local HH:MM, recurs daily
    power: bool = True
    operation_mode: Optional[str] = None
    set_temperature: Optional[float] = None
    fan_speed: Optional[str] = None
    vane_vertical_direction: Optional[str] = None
    vane_horizontal_direction: Optional[str] = None


# Backwards-compatible name for old imports/type hints. New code should use
# ScheduleEntry because a unit now owns a list of entries.
Schedule = ScheduleEntry


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


#: How far past the target the idle setpoint sits (°C). One degree on the
#: satisfied side is enough for the unit's own thermostat to stop actively
#: driving while the unit stays on.
IDLE_OFFSET = 1.0


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
    """One adjustment of the unit's setpoint toward the desired room ``target``.

    Asymmetric by design:

    * **Drive-harder side** (room past the target by more than ``buffer``) moves
      the setpoint one ``step`` per call, so the slow-responding room has time to
      react before the next adjustment and a genuinely hot/cold room ramps in
      steadily.
    * **Satisfied side** (room has reached the target) jumps the setpoint
      *immediately* to one ``IDLE_OFFSET`` degree on the satisfied side of the
      target (Cool: ``target + 1``; Heat: ``target - 1``). The unit's own
      thermostat then idles — it stays on but stops actively driving — instead of
      clawing back one step at a time, which would park the setpoint at an
      extreme and overshoot the room deep past the target during the slow
      recovery.

    Returns the new setpoint, or ``None`` to hold (room inside the deadband
    between the target and ``target ± buffer``, an un-steerable mode, missing
    readings, or the result already equals the current clamped setpoint).
    """
    if room_temperature is None or set_temperature is None or target is None:
        return None

    if operation_mode in COOL_MODES:
        if room_temperature > target + buffer:
            new = set_temperature - step  # too warm → cool harder (gradual)
        elif room_temperature <= target:
            new = target + IDLE_OFFSET  # reached target → idle immediately
        else:
            return None  # (target, target+buffer] deadband → hold
    elif operation_mode in HEAT_MODES:
        if room_temperature < target - buffer:
            new = set_temperature + step  # too cold → heat harder (gradual)
        elif room_temperature >= target:
            new = target - IDLE_OFFSET  # reached target → idle immediately
        else:
            return None  # [target-buffer, target) deadband → hold
    else:
        return None

    new = max(tmin, min(tmax, new))
    if new == set_temperature:
        return None
    return new


# --------------------------------------------------------------- persistence
def _read_json(path: Path) -> Any:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("⚠️ Could not read %s (%s); returning empty", path, exc)
        return {}


def _load(path: Path) -> Dict[str, dict]:
    raw = _read_json(path)
    if not isinstance(raw, dict):
        logger.warning("⚠️ %s is not a JSON object; returning empty", path)
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


def _save(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    logger.info("💾 Saved %s", path)


def _clean_entry(raw: dict, fallback_id: str) -> ScheduleEntry:
    """Coerce untrusted JSON/API data into a ScheduleEntry."""
    allowed = {
        "id",
        "enabled",
        "time",
        "power",
        "operation_mode",
        "set_temperature",
        "fan_speed",
        "vane_vertical_direction",
        "vane_horizontal_direction",
    }
    data = {k: raw[k] for k in allowed if k in raw}
    data["id"] = str(data.get("id") or fallback_id)
    # Keep ids compact/safe for DOM keys and fire-state keys. If a client sends
    # something odd, preserve uniqueness but remove whitespace/control chars.
    data["id"] = "-".join(data["id"].split()) or fallback_id
    return ScheduleEntry(**data)


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


def load_schedules(path: Optional[Path] = None) -> Dict[str, List[ScheduleEntry]]:
    """Return {unit_id: [ScheduleEntry, ...]} from disk, or {} if absent.

    Backward-compatible with the issue-83 shape where each unit mapped directly
    to one schedule object. Legacy entries load with id ``default``.
    """
    target = Path(path) if path is not None else SCHEDULES_PATH
    raw = _read_json(target)
    if not isinstance(raw, dict):
        logger.warning("⚠️ %s is not a JSON object; returning empty", target)
        return {}

    out: Dict[str, List[ScheduleEntry]] = {}
    for uid, value in raw.items():
        entries: List[ScheduleEntry] = []
        if isinstance(value, list):
            for idx, item in enumerate(value, start=1):
                if isinstance(item, dict):
                    entries.append(_clean_entry(item, f"schedule-{idx}"))
        elif isinstance(value, dict):
            # Legacy single-schedule object.
            entries.append(_clean_entry(value, "default"))
        if entries:
            out[str(uid)] = entries
    return out


def save_schedules(
    schedules: Dict[str, List[ScheduleEntry]],
    path: Optional[Path] = None,
) -> None:
    """Atomically persist the whole schedule map."""
    target = Path(path) if path is not None else SCHEDULES_PATH
    payload = {
        uid: [asdict(s) for s in entries]
        for uid, entries in schedules.items()
        if entries
    }
    _save(target, payload)


def set_schedules(
    unit_id: str,
    entries: List[ScheduleEntry],
    path: Optional[Path] = None,
) -> None:
    """Replace one unit's schedule-entry list (empty list removes it)."""
    schedules = load_schedules(path)
    clean = [entry for entry in entries if entry.id]
    if clean:
        schedules[unit_id] = clean
    else:
        schedules.pop(unit_id, None)
    save_schedules(schedules, path)


def set_schedule(unit_id: str, schedule: ScheduleEntry, path: Optional[Path] = None) -> None:
    """Backward-compatible single-entry setter used by old clients/tests."""
    if schedule.enabled:
        set_schedules(unit_id, [schedule], path)
    else:
        set_schedules(unit_id, [], path)
