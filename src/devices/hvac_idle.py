"""Catch a unit that reports on but stays idle, whatever put it there.

The schedule check in :mod:`src.devices.hvac_verify` only watches the minutes
after a schedule fire. A unit can also stick later: ``Power`` reads on (it
echoes the cloud-side request), the unit's own ``ActualFanSpeed`` reads
``Off``, and the room does not move toward the setpoint. The temperature rule
then keeps raising the setpoint of a unit that is not running at all.

:class:`IdleTracker` is the pure detector, fed one read per engine tick: a unit
in heat or cool whose fan stays off for ``window_s`` while the room is at least
``min_gap_c`` short of the setpoint and gains less than ``min_progress_c`` on
it is *flagged*. A flagged unit gets one power cycle and one read window
(:func:`src.devices.hvac_verify.cycle_and_watch`, the schedule retry's path),
then one home alert if it still has not started. The flag holds until a read
shows the unit running, satisfied, off or in another mode, so a unit that never
recovers is acted on once, never in a loop; ``cooldown_s`` also spaces actions
on a unit that keeps relapsing.

UI-free and clock-injected: the engine passes ``time.monotonic()`` readings
and the MELCloud/notifier seams, tests pass fakes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from src._env import _env_bool, _env_float
from src.devices.hvac_verify import (
    FAN_OFF,
    FetchFn,
    NotifyFn,
    RecordFn,
    Request,
    SleepFn,
    Verdict,
    VerifyConfig,
    WriteFn,
    cycle_and_watch,
)
from src.devices.melcloud_client import DeviceInfo

logger = logging.getLogger(__name__)

# The issue's two failure modes; Dry, Auto and Fan idle or move air on purpose.
_WATCHED_MODES = ("Heat", "Cool")


@dataclass(frozen=True)
class IdleConfig:
    """Idle-detector knobs, loaded from ``.env`` (all optional)."""

    enabled: bool = True
    # How long the fan must stay off with the room not closing in. Longer than
    # a defrost or a heat start's coil warm-up, which also stop the fan.
    window_s: float = 1200.0
    # A unit's own thermostat may rest the fan within about a degree of the
    # setpoint, so a smaller shortfall is never treated as stuck.
    min_gap_c: float = 1.0
    # Room movement toward the setpoint that counts as the unit working; one
    # MELCloud room-reading step.
    min_progress_c: float = 0.5
    # Minimum gap between two power cycles on the same unit.
    cooldown_s: float = 3600.0


def load_idle_config() -> IdleConfig:
    """Read the idle-detector knobs from the environment (``.env`` already loaded)."""
    return IdleConfig(
        enabled=_env_bool("HVAC_IDLE_ENABLED", True),
        window_s=max(60.0, _env_float("HVAC_IDLE_WINDOW_S", IdleConfig.window_s)),
        min_gap_c=max(0.0, _env_float("HVAC_IDLE_MIN_GAP_C", IdleConfig.min_gap_c)),
        min_progress_c=max(
            0.0, _env_float("HVAC_IDLE_MIN_PROGRESS_C", IdleConfig.min_progress_c)
        ),
        cooldown_s=max(0.0, _env_float("HVAC_IDLE_COOLDOWN_S", IdleConfig.cooldown_s)),
    )


def _shortfall(unit: DeviceInfo) -> Optional[float]:
    """How far the room is from the setpoint in the direction the mode drives it."""
    room, setpoint = unit.room_temperature, unit.set_temperature
    if room is None or setpoint is None:
        return None
    return setpoint - room if unit.operation_mode == "Heat" else room - setpoint


def looks_idle(unit: DeviceInfo, config: IdleConfig) -> bool:
    """One read of a unit that is on, needs to run, and is not running."""
    if unit.power is not True or not unit.reachable:
        return False
    if unit.operation_mode not in _WATCHED_MODES or unit.actual_fan_speed != FAN_OFF:
        return False
    shortfall = _shortfall(unit)
    return shortfall is not None and shortfall >= config.min_gap_c


@dataclass
class _Episode:
    since: float  # monotonic time the fan was first seen off with the room short
    mode: str
    baseline_room: float
    flagged: bool = False


class IdleTracker:
    """Per-unit idle episodes across engine ticks."""

    def __init__(self) -> None:
        self._episodes: Dict[str, _Episode] = {}
        self._last_action: Dict[str, float] = {}

    def is_flagged(self, unit_id: str) -> bool:
        episode = self._episodes.get(unit_id)
        return bool(episode and episode.flagged)

    def reset(self, unit_id: str) -> None:
        """Forget a unit's episode (a schedule just rewrote it)."""
        self._episodes.pop(unit_id, None)

    def observe(self, unit: DeviceInfo, now: float, config: IdleConfig) -> bool:
        """Feed one read; True exactly when this read starts an action on the unit."""
        uid = unit.unit_id
        if not looks_idle(unit, config):
            if self._episodes.pop(uid, None) is not None:
                logger.info("ℹ️ HVAC '%s' no longer idle: %s", unit.name, _brief(unit))
            return False
        room = float(unit.room_temperature)  # looks_idle guarantees a reading
        mode = str(unit.operation_mode)
        episode = self._episodes.get(uid)
        if episode is None or episode.mode != mode:
            self._episodes[uid] = _Episode(since=now, mode=mode, baseline_room=room)
            return False
        if episode.flagged:
            return False
        gained = room - episode.baseline_room if mode == "Heat" else episode.baseline_room - room
        if gained >= config.min_progress_c:
            # The room is closing in: whatever the fan field says, it is working.
            self._episodes[uid] = _Episode(since=now, mode=mode, baseline_room=room)
            return False
        if now - episode.since < config.window_s:
            return False
        episode.flagged = True
        last = self._last_action.get(uid)
        if last is not None and now - last < config.cooldown_s:
            logger.warning(
                "⚠️ HVAC '%s' idle again %.0f min after its last power cycle — "
                "flagged, not cycled (cooldown)", unit.name, (now - last) / 60,
            )
            return False
        self._last_action[uid] = now
        logger.warning(
            "⚠️ HVAC '%s' reports on but idle for %.0f min: %s",
            unit.name, (now - episode.since) / 60, _brief(unit),
        )
        return True


@dataclass
class IdleResult:
    """How one idle action ended, for the caller's log and for tests."""

    verdict: Verdict
    last_unit: Optional[DeviceInfo]
    alerted: bool = False


async def recover_idle_unit(
    unit: DeviceInfo,
    verify: VerifyConfig,
    *,
    fetch: FetchFn,
    write: WriteFn,
    sleep: SleepFn,
    notify: NotifyFn,
    record: RecordFn,
) -> IdleResult:
    """Power-cycle an idle-flagged unit once, watch it, alert once if it stays idle.

    The cycle re-applies the unit's own reported mode and setpoint, so it asks
    for nothing the unit was not already set to. Never raises.
    """
    request = Request(
        unit_id=unit.unit_id,
        unit_name=unit.name,
        sched_time="idle",
        profile={
            "power": True,
            "operation_mode": unit.operation_mode,
            "set_temperature": unit.set_temperature,
        },
    )
    _record(record, unit, "cycle", "warning", unit)
    verdict, last = await cycle_and_watch(request, verify, fetch=fetch, write=write, sleep=sleep)
    result = IdleResult(verdict=verdict, last_unit=last)
    if verdict in (Verdict.CONFIRMED, Verdict.SATISFIED):
        logger.info("✅ HVAC '%s' running again after one power cycle", unit.name)
        _record(record, unit, "recovered", "info", last)
        return result
    if verdict is Verdict.SUPERSEDED:
        logger.info("ℹ️ HVAC '%s' changed by someone else during the idle check", unit.name)
        _record(record, unit, "superseded", "info", last)
        return result
    if verdict is Verdict.READ_FAILED:
        text = (
            f"⚠️ HVAC '{unit.name}' reported on but idle; power-cycled once, but MELCloud "
            f"gave no usable readback afterwards — could not confirm it started."
        )
        outcome = "unverified"
    else:
        text = (
            f"⚠️ HVAC '{unit.name}' reports on but is not running: power-cycled once and "
            f"still idle. Read: {_brief(last)}."
        )
        outcome = "failed"
    logger.warning(text)
    _record(record, unit, outcome, "warning", last)
    try:
        await notify(text)
        result.alerted = True
    except Exception as exc:  # noqa: BLE001 — delivery must never break the engine
        logger.warning("⚠️ HVAC idle alert failed: %s", exc)
    return result


def _record(
    record: RecordFn, unit: DeviceInfo, outcome: str, severity: str, read: Optional[DeviceInfo]
) -> None:
    """Mirror one idle step to the activity feed (best effort)."""
    try:
        record(
            "hvac", "idle_check", entity_id=unit.unit_id, source="hvac_automation",
            outcome=outcome, severity=severity, payload={"read": _brief(read)},
        )
    except Exception as exc:  # noqa: BLE001 — the feed is a breadcrumb, never fatal
        logger.warning("⚠️ HVAC idle activity record failed: %s", exc)


def _brief(unit: Optional[Any]) -> str:
    if unit is None:
        return "no readback"
    return (
        f"mode {unit.operation_mode}, fan {unit.actual_fan_speed}, "
        f"room {unit.room_temperature}°C, setpoint {unit.set_temperature}°C"
    )
