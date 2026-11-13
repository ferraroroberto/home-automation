"""Verify an HVAC schedule fire against what the unit actually does.

MELCloud's ``Power`` readback echoes the cloud-side request: a unit that never
received a power-on still reads ``Power=True``. ``ActualFanSpeed`` is reported
by the unit itself (it differs from ``SetFanSpeed`` on a running unit and reads
``Off`` on a stopped one), so it is the readback that can tell "requested"
from "running" — see ``docs/hvac.md`` § "Schedule verification".

The fan legitimately stops for a few minutes after a heat start (coil
warm-up), during defrost, and once the room has reached the setpoint, so a
fire is judged over a window of several reads rather than one, and a stopped
fan in a satisfied room is "unconfirmed", never a failure.

UI-free: the fetch, the write, the clock, the notifier and the activity-feed
recorder are all injected, so tests drive every path with fakes.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from src._env import _env_bool, _env_float, _env_int
from src.devices.melcloud_client import DeviceInfo

logger = logging.getLogger(__name__)

FAN_OFF = "Off"
# Auto mode heats or cools toward the setpoint, so within this band of it the
# unit may be idling on purpose.
_AUTO_SATISFIED_BAND_C = 1.0


@dataclass(frozen=True)
class VerifyConfig:
    """Verification knobs, loaded from ``.env`` (all optional)."""

    enabled: bool = True
    # Seconds after a write at which the unit is read. The first read waits
    # out a heat start's coil warm-up; the last outlasts a defrost cycle.
    read_offsets_s: Tuple[float, ...] = (180.0, 300.0, 480.0)
    retries: int = 2
    # Wait before retry N (the last value repeats if there are more retries).
    backoff_s: Tuple[float, ...] = (60.0, 120.0)
    # Off-to-on gap of a retry's power cycle; a few seconds is what cleared a
    # stuck unit by hand.
    cycle_pause_s: float = 10.0


def _env_seconds_list(name: str, default: Tuple[float, ...]) -> Tuple[float, ...]:
    """Parse a comma-separated list of non-negative seconds (blank → default)."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        values = tuple(float(part) for part in raw.split(",") if part.strip())
    except ValueError:
        logger.warning("⚠️ Invalid %s=%s; using %s", name, raw, default)
        return default
    if not values or any(v < 0 for v in values):
        logger.warning("⚠️ Invalid %s=%s; using %s", name, raw, default)
        return default
    return values


def load_verify_config() -> VerifyConfig:
    """Read the verification knobs from the environment (``.env`` already loaded)."""
    offsets = tuple(sorted(_env_seconds_list("HVAC_VERIFY_READS_S", VerifyConfig.read_offsets_s)))
    return VerifyConfig(
        enabled=_env_bool("HVAC_VERIFY_ENABLED", True),
        read_offsets_s=offsets,
        retries=_env_int("HVAC_VERIFY_RETRIES", VerifyConfig.retries, non_negative=True),
        backoff_s=_env_seconds_list("HVAC_VERIFY_BACKOFF_S", VerifyConfig.backoff_s),
        cycle_pause_s=max(0.0, _env_float("HVAC_VERIFY_CYCLE_PAUSE_S", VerifyConfig.cycle_pause_s)),
    )


class Verdict(str, Enum):
    """What one read (or one whole watch window) says about the fire."""

    CONFIRMED = "confirmed"  # the unit is doing what was asked
    SATISFIED = "unconfirmed"  # fan stopped, but the room is already at the setpoint
    SUPERSEDED = "superseded"  # power/mode changed since the write: someone else's call
    NOT_RUNNING = "not_running"  # asked on, reads on, fan stopped in a room that needs it
    STILL_RUNNING = "still_running"  # asked off, reads off, fan still turning
    READ_FAILED = "read_failed"  # no usable readback: fetch error, unit missing, field absent


@dataclass(frozen=True)
class Request:
    """The state a schedule fire asked for, and the full write that applies it."""

    unit_id: str
    unit_name: str
    sched_time: str
    profile: Dict[str, Any]  # set_device_state kwargs; always carries ``power``

    @property
    def power(self) -> bool:
        return bool(self.profile.get("power"))

    @property
    def operation_mode(self) -> Optional[str]:
        return self.profile.get("operation_mode")


def _thermo_satisfied(unit: DeviceInfo) -> bool:
    """True when a stopped fan is the unit idling at its setpoint, not a fault."""
    room, setpoint, mode = unit.room_temperature, unit.set_temperature, unit.operation_mode
    if room is None or setpoint is None:
        return False
    if mode == "Heat":
        return room >= setpoint
    if mode in ("Cool", "Dry"):
        return room <= setpoint
    if mode == "Automatic":
        return abs(room - setpoint) <= _AUTO_SATISFIED_BAND_C
    return False  # Fan mode always moves air


def classify(request: Request, unit: Optional[DeviceInfo]) -> Verdict:
    """Judge one readback of the unit against the request."""
    if unit is None or unit.power is None:
        return Verdict.READ_FAILED
    if unit.power != request.power:
        return Verdict.SUPERSEDED
    if request.power and request.operation_mode and unit.operation_mode != request.operation_mode:
        return Verdict.SUPERSEDED
    if unit.actual_fan_speed is None:
        return Verdict.READ_FAILED
    fan_turning = unit.actual_fan_speed != FAN_OFF
    if not request.power:
        return Verdict.STILL_RUNNING if fan_turning else Verdict.CONFIRMED
    if fan_turning:
        return Verdict.CONFIRMED
    return Verdict.SATISFIED if _thermo_satisfied(unit) else Verdict.NOT_RUNNING


_FINAL = (Verdict.CONFIRMED, Verdict.SUPERSEDED)
_FAILED = (Verdict.NOT_RUNNING, Verdict.STILL_RUNNING)

FetchFn = Callable[[], Awaitable[List[DeviceInfo]]]
WriteFn = Callable[..., Awaitable[Any]]
SleepFn = Callable[[float], Awaitable[Any]]
NotifyFn = Callable[[str], Awaitable[None]]
RecordFn = Callable[..., None]


@dataclass
class VerifyResult:
    """How a verification ended, for the caller's log and for tests."""

    verdict: Verdict
    attempts: int  # writes made, counting the schedule's own
    last_unit: Optional[DeviceInfo]
    alerted: bool = False


class _Verifier:
    """One fire's verification run; holds the injected seams."""

    def __init__(
        self,
        request: Request,
        config: VerifyConfig,
        *,
        fetch: FetchFn,
        write: WriteFn,
        sleep: SleepFn,
        notify: NotifyFn,
        record: RecordFn,
    ) -> None:
        self.request = request
        self.config = config
        self.fetch = fetch
        self.write = write
        self.sleep = sleep
        self.notify = notify
        self.record = record
        self.last_unit: Optional[DeviceInfo] = None

    async def read(self) -> Verdict:
        """One fresh read of the unit, classified and logged."""
        unit: Optional[DeviceInfo] = None
        try:
            devices = await self.fetch()
            unit = next((d for d in devices if d.unit_id == self.request.unit_id), None)
        except Exception as exc:  # noqa: BLE001 — a failed read is a verdict, not a crash
            logger.warning(
                "⚠️ HVAC verify read failed for '%s': %s", self.request.unit_name, exc
            )
        if unit is not None:
            self.last_unit = unit
        verdict = classify(self.request, unit)
        logger.info(
            "🔎 HVAC verify '%s' (%s schedule): %s — %s",
            self.request.unit_name, self.request.sched_time, verdict.value, _describe(unit),
        )
        return verdict

    async def watch(self) -> Verdict:
        """Read at each offset after a write; the first decisive read ends it.

        Otherwise the window's verdict is its last usable read, so a heat start
        that warms up late still counts and an all-failed window stays
        ``READ_FAILED`` rather than reading as a fault.
        """
        elapsed = 0.0
        last_usable: Optional[Verdict] = None
        for offset in self.config.read_offsets_s:
            await self.sleep(max(0.0, offset - elapsed))
            elapsed = max(elapsed, offset)
            verdict = await self.read()
            if verdict in _FINAL:
                return verdict
            if verdict is not Verdict.READ_FAILED:
                last_usable = verdict
        return last_usable or Verdict.READ_FAILED

    async def resend(self) -> None:
        """Re-apply the request: a power cycle to start a unit, a plain re-send to stop it."""
        if self.request.power:
            await self.write(self.request.unit_id, power=False)
            await self.sleep(self.config.cycle_pause_s)
        await self.write(self.request.unit_id, **self.request.profile)

    def backoff(self, retry: int) -> float:
        delays = self.config.backoff_s or (0.0,)
        return delays[min(retry, len(delays) - 1)]


async def verify_schedule_fire(
    request: Request,
    config: VerifyConfig,
    *,
    fetch: FetchFn,
    write: WriteFn,
    sleep: SleepFn,
    notify: NotifyFn,
    record: RecordFn,
) -> VerifyResult:
    """Watch a schedule fire; retry a unit that did not follow; alert once at the cap.

    A retry only follows a window that saw the unit *disobey* — after a window
    of failed reads the state is unknown, so nothing is re-sent and the next
    window just reads again. Before each retry the unit is read once more, so a
    manual change made during the backoff is respected instead of undone.
    Never re-fires the schedule itself and never raises.
    """
    v = _Verifier(request, config, fetch=fetch, write=write, sleep=sleep, notify=notify, record=record)
    attempts = 1
    verdict = await v.watch()
    for retry in range(config.retries):
        if verdict not in _FAILED and verdict is not Verdict.READ_FAILED:
            break
        await v.sleep(v.backoff(retry))
        if verdict in _FAILED:
            fresh = await v.read()
            if fresh not in _FAILED:
                verdict = fresh
                if fresh is not Verdict.READ_FAILED:
                    break
            else:
                _record(v, "retry", "warning", attempt=attempts + 1)
                logger.warning(
                    "⚠️ HVAC schedule %s for '%s' not followed (%s) — %s, attempt %d",
                    request.sched_time, request.unit_name, verdict.value,
                    "power-cycling" if request.power else "re-sending off", attempts + 1,
                )
                try:
                    await v.resend()
                except Exception as exc:  # noqa: BLE001 — a failed re-send still counts
                    logger.warning(
                        "⚠️ HVAC verify re-send failed for '%s': %s", request.unit_name, exc
                    )
                attempts += 1
        verdict = await v.watch()

    result = VerifyResult(verdict=verdict, attempts=attempts, last_unit=v.last_unit)
    await _conclude(v, result)
    return result


async def _conclude(v: _Verifier, result: VerifyResult) -> None:
    """Log and record the outcome; alert once when the unit never followed."""
    req, verdict = v.request, result.verdict
    if verdict is Verdict.CONFIRMED:
        logger.info(
            "✅ HVAC schedule %s for '%s' verified (attempt %d)",
            req.sched_time, req.unit_name, result.attempts,
        )
        _record(v, "confirmed", "info", attempts=result.attempts)
        return
    if verdict is Verdict.SUPERSEDED:
        logger.info(
            "ℹ️ HVAC schedule %s for '%s' superseded by a later change — not verified",
            req.sched_time, req.unit_name,
        )
        _record(v, "superseded", "info", attempts=result.attempts)
        return
    if verdict is Verdict.SATISFIED:
        logger.info(
            "ℹ️ HVAC schedule %s for '%s' unconfirmed: fan stopped with the room at the setpoint",
            req.sched_time, req.unit_name,
        )
        _record(v, "unconfirmed", "info", attempts=result.attempts)
        return
    if verdict is Verdict.READ_FAILED:
        text = (
            f"⚠️ HVAC schedule {req.sched_time} for '{req.unit_name}': could not verify — "
            f"MELCloud gave no usable readback. Asked: {_describe_request(req)}."
        )
        outcome = "unverified"
    else:
        text = (
            f"⚠️ HVAC schedule {req.sched_time} for '{req.unit_name}' did not take effect "
            f"after {result.attempts} attempt(s)"
            f"{' (power-cycled on each retry)' if req.power and result.attempts > 1 else ''}. "
            f"Asked: {_describe_request(req)}. Read: {_describe(result.last_unit)}."
        )
        outcome = "failed"
    logger.warning(text)
    _record(v, outcome, "warning", attempts=result.attempts)
    try:
        await v.notify(text)
        result.alerted = True
    except Exception as exc:  # noqa: BLE001 — delivery must never break the engine
        logger.warning("⚠️ HVAC verify alert failed: %s", exc)


def _record(v: _Verifier, outcome: str, severity: str, **extra: Any) -> None:
    """Mirror one verification step to the activity feed (best effort)."""
    unit = v.last_unit
    payload: Dict[str, Any] = {
        "schedule_time": v.request.sched_time,
        "asked": _describe_request(v.request),
        "read": _describe(unit),
        **extra,
    }
    try:
        v.record(
            "hvac", "schedule_verify", entity_id=v.request.unit_id,
            source="hvac_automation", outcome=outcome, severity=severity, payload=payload,
        )
    except Exception as exc:  # noqa: BLE001 — the feed is a breadcrumb, never fatal
        logger.warning("⚠️ HVAC verify activity record failed: %s", exc)


def _describe_request(req: Request) -> str:
    parts = ["on" if req.power else "off"]
    if req.power:
        if req.operation_mode:
            parts.append(req.operation_mode)
        if req.profile.get("set_temperature") is not None:
            parts.append(f"{req.profile['set_temperature']}°C")
    return " ".join(parts)


def _describe(unit: Optional[DeviceInfo]) -> str:
    if unit is None:
        return "no readback"
    power = {True: "on", False: "off"}.get(unit.power, "unknown")  # type: ignore[arg-type]
    return (
        f"power {power}, mode {unit.operation_mode}, fan {unit.actual_fan_speed}, "
        f"room {unit.room_temperature}°C, setpoint {unit.set_temperature}°C"
        f"{'' if unit.reachable else ', offline'}"
    )
