"""Unit read + control API over the MELCloud Home core.

``GET /api/units`` returns every air-to-air unit's live state; ``POST
/api/units/{id}`` writes the changed controls and returns the read-back
snapshot so the client can re-render just that card.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from aiomelcloudhome import ATAFanSpeed, ATAOperationMode, ATAVaneHorizontal, ATAVaneVertical
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError, field_validator

from app.webapp import automation
from app.webapp.routers._helpers import make_display_name_endpoint
from src._schedule_store import StoreUnreadableError
from src.display_names import load_display_names, set_display_name
from src.hvac_automation import (
    MIN_SETTLE_INTERVAL_S,
    ORDERING_POLICIES,
    BoostCoordinatorConfig,
    ScheduleEntry,
    TempRule,
    boosted_target,
    load_boost_config,
    load_rules,
    load_schedules,
    save_boost_config,
    set_rule,
    set_schedules,
    target_for_mode,
)
from src.melcloud_client import (
    DeviceInfo,
    DeviceNotFoundError,
    MelCloudConfigError,
    fetch_devices,
    set_device_state,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _entry_dict(entry: ScheduleEntry) -> Dict[str, Any]:
    return {
        "id": entry.id,
        "enabled": entry.enabled,
        "time": entry.time,
        "power": entry.power,
        "operation_mode": entry.operation_mode,
        "set_temperature": entry.set_temperature,
        "target_temperature": entry.target_temperature,
        "fan_speed": entry.fan_speed,
        "vane_vertical_direction": entry.vane_vertical_direction,
        "vane_horizontal_direction": entry.vane_horizontal_direction,
    }


def _next_schedule_time(entries: List[ScheduleEntry]) -> Optional[str]:
    enabled = sorted(e.time for e in entries if e.enabled and e.time)
    if not enabled:
        return None
    now_hhmm = datetime.now().strftime("%H:%M")
    for hhmm in enabled:
        if hhmm >= now_hhmm:
            return hhmm
    return enabled[0]


def _schedule_summary(entries: Optional[List[ScheduleEntry]]) -> Dict[str, Any]:
    items = entries or []
    enabled_count = sum(1 for e in items if e.enabled)
    return {
        "enabled": enabled_count > 0,
        "count": enabled_count,
        "next_time": _next_schedule_time(items),
        # Compatibility with the issue-83 tile shape / old PWA shells.
        "time": _next_schedule_time(items),
    }


def _unit_dict(
    d: DeviceInfo,
    display_names: Optional[Dict[str, str]] = None,
    rules: Optional[Dict[str, TempRule]] = None,
    schedules: Optional[Dict[str, List[ScheduleEntry]]] = None,
) -> Dict[str, Any]:
    """Flatten a :class:`DeviceInfo` into a JSON-serialisable dict."""
    overrides = display_names or {}
    rule = (rules or {}).get(d.unit_id)
    schedule_entries = (schedules or {}).get(d.unit_id, [])
    rule_target = target_for_mode(rule, d.operation_mode) if rule is not None else None
    boost_active = automation.get_boost_active(d.unit_id)
    # Signed delta the running engine is actually applying (#575) — reuses
    # boosted_target() so the UI never re-derives the cool/heat sign itself.
    boost_delta_c: Optional[float] = None
    if boost_active and rule is not None and rule_target is not None:
        effective_target = boosted_target(
            operation_mode=d.operation_mode,
            target=rule_target,
            boost_offset_c=rule.boost_offset_c,
            is_boosting=True,
        )
        if effective_target is not None:
            boost_delta_c = effective_target - rule_target
    return {
        "unit_id": d.unit_id,
        "name": d.name,
        "display_name": overrides.get(d.unit_id) or None,
        "building": d.building,
        "power": d.power,
        "operation_mode": d.operation_mode,
        "room_temperature": d.room_temperature,
        "set_temperature": d.set_temperature,
        "fan_speed": d.fan_speed,
        # False when the unit's WiFi adapter has lost its cloud connection —
        # the PWA dims the card and inerts its controls (issue #520).
        "reachable": d.reachable,
        "operation_modes": d.operation_modes,
        "fan_speeds": d.fan_speeds,
        "temp_step": d.temp_step,
        # temp_ranges maps mode -> [min, max] (tuple → list for JSON).
        "temp_ranges": {k: list(v) for k, v in d.temp_ranges.items()},
        "vane_vertical": d.vane_vertical,
        "vane_horizontal": d.vane_horizontal,
        "vane_vertical_options": d.vane_vertical_options,
        "vane_horizontal_options": d.vane_horizontal_options,
        "has_vane_vertical": d.has_vane_vertical,
        "has_vane_horizontal": d.has_vane_horizontal,
        "temperature_rule": {
            "enabled": bool(rule and rule.enabled),
            "active_target": rule_target,
            "boost_enabled": bool(rule and rule.boost_enabled),
            "boost_offset_c": rule.boost_offset_c if rule else None,
            # Live — whether the running automation engine currently has this
            # unit solar-boosted (#554), not a persisted field.
            "boost_active": boost_active,
            # Signed delta actually applied while boosting (#575) — negative
            # when cooling, positive when heating; None when not boosting.
            "boost_delta_c": boost_delta_c,
        },
        "schedule": _schedule_summary(schedule_entries),
    }


def _decoration(load, label: str, empty):
    """Read a store that only *decorates* a unit payload, never decides on it.

    These reads are display enrichment layered onto the live MELCloud device
    list, and they are never written back — so a momentarily unreadable file
    (issue #689) degrades to "no rules/schedules shown this poll" with a logged
    warning, instead of 500-ing `GET /api/units` and taking the PWA home grid
    and the Home Assistant `climate` platform down over a file that has nothing
    to do with the device list. Decision paths deliberately do the opposite and
    refuse to act.
    """

    try:
        return load()
    except StoreUnreadableError as exc:
        logger.warning("⚠️ %s unreadable this poll; showing none (%s)", label, exc)
        return empty


@router.get("/api/units")
async def list_units() -> Dict[str, Any]:
    try:
        devices = await fetch_devices()
    except MelCloudConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — surface any API/network error
        logger.warning("⚠️  Failed to fetch units: %s", exc)
        raise HTTPException(status_code=502, detail=f"failed to fetch units: {exc}")
    display_names = load_display_names()
    rules = _decoration(load_rules, "config/hvac_rules.json", {})
    schedules = _decoration(load_schedules, "config/hvac_schedules.json", {})
    return {"units": [_unit_dict(d, display_names, rules, schedules) for d in devices]}


# ── valid enum values for the control body validator ──────────────────────────
_VALID_OPERATION_MODES: frozenset[str] = frozenset(m.value for m in ATAOperationMode)
_VALID_FAN_SPEEDS: frozenset[str] = frozenset(f.value for f in ATAFanSpeed)
_VALID_VANE_VERTICAL: frozenset[str] = frozenset(v.value for v in ATAVaneVertical)
_VALID_VANE_HORIZONTAL: frozenset[str] = frozenset(v.value for v in ATAVaneHorizontal)


class ControlPayload(BaseModel):
    """Validated body for ``POST /api/units/{unit_id}``.

    All fields are optional so the client can send only what changed.
    Unknown or non-coercible values return 422 (Unprocessable Entity) rather
    than reaching the hardware as a 502.  ``set_temperature`` is accepted as
    any float here; per-mode clamping to the unit's ``temp_ranges`` is applied
    inside ``set_device_state`` before the write reaches ``control_ata_unit``.
    """

    power: Optional[bool] = None
    operation_mode: Optional[str] = None
    set_temperature: Optional[float] = None
    fan_speed: Optional[str] = None
    vane_vertical_direction: Optional[str] = None
    vane_horizontal_direction: Optional[str] = None

    @field_validator("operation_mode")
    @classmethod
    def _check_operation_mode(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in _VALID_OPERATION_MODES:
            raise ValueError(
                f"operation_mode must be one of {sorted(_VALID_OPERATION_MODES)}"
            )
        return v

    @field_validator("fan_speed")
    @classmethod
    def _check_fan_speed(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in _VALID_FAN_SPEEDS:
            raise ValueError(f"fan_speed must be one of {sorted(_VALID_FAN_SPEEDS)}")
        return v

    @field_validator("vane_vertical_direction")
    @classmethod
    def _check_vane_vertical(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in _VALID_VANE_VERTICAL:
            raise ValueError(
                f"vane_vertical_direction must be one of {sorted(_VALID_VANE_VERTICAL)}"
            )
        return v

    @field_validator("vane_horizontal_direction")
    @classmethod
    def _check_vane_horizontal(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in _VALID_VANE_HORIZONTAL:
            raise ValueError(
                f"vane_horizontal_direction must be one of {sorted(_VALID_VANE_HORIZONTAL)}"
            )
        return v


make_display_name_endpoint(
    router, "/api/units/{item_id}/display_name", "unit_id", set_display_name
)


@router.post("/api/units/{unit_id}")
async def control_unit(unit_id: str, payload: ControlPayload) -> Dict[str, Any]:
    # Only forward the fields the client actually sent (non-None values).
    # FastAPI / Pydantic already rejected malformed types with 422 before we get here.
    kwargs = payload.model_dump(exclude_none=True)
    try:
        updated = await set_device_state(unit_id, **kwargs)
    except MelCloudConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except DeviceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — surface any API/network error
        logger.warning("⚠️  Failed to control unit %s: %s", unit_id, exc)
        raise HTTPException(status_code=502, detail=f"failed to apply: {exc}")
    display_names = load_display_names()
    rules = _decoration(load_rules, "config/hvac_rules.json", {})
    schedules = _decoration(load_schedules, "config/hvac_schedules.json", {})
    return _unit_dict(updated, display_names, rules, schedules)


class RulePayload(BaseModel):
    """Dynamic-setpoint rule for one unit (per-mode desired room temps)."""

    enabled: bool = False
    cool_target: Optional[float] = None
    heat_target: Optional[float] = None
    boost_enabled: bool = False
    boost_offset_c: float = 2.0


class ScheduleEntryPayload(BaseModel):
    """One daily schedule entry for a unit."""

    id: Optional[str] = None
    enabled: bool = True
    time: str = "08:00"
    power: bool = True
    operation_mode: Optional[str] = None
    set_temperature: Optional[float] = None
    target_temperature: Optional[float] = None
    fan_speed: Optional[str] = None
    vane_vertical_direction: Optional[str] = None
    vane_horizontal_direction: Optional[str] = None


def _new_schedule_id() -> str:
    return "sched-" + uuid.uuid4().hex[:10]


def _coerce_schedule_entries(body: Any) -> List[ScheduleEntry]:
    """Accept the new {entries:[...]} shape and the old single-object shape."""
    if isinstance(body, dict) and isinstance(body.get("entries"), list):
        raw_entries = body["entries"]
    elif isinstance(body, list):
        raw_entries = body
    elif isinstance(body, dict):
        raw_entries = [body]
    else:
        raise HTTPException(status_code=400, detail="expected a JSON object or list")

    entries: List[ScheduleEntry] = []
    seen: set[str] = set()
    for idx, raw in enumerate(raw_entries, start=1):
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="schedule entries must be objects")
        try:
            payload = ScheduleEntryPayload(**raw)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        sid = (payload.id or _new_schedule_id()).strip() or _new_schedule_id()
        while sid in seen:
            sid = _new_schedule_id()
        seen.add(sid)
        entries.append(
            ScheduleEntry(
                id=sid,
                enabled=payload.enabled,
                time=payload.time or "08:00",
                power=payload.power,
                operation_mode=payload.operation_mode,
                set_temperature=payload.set_temperature,
                target_temperature=payload.target_temperature,
                fan_speed=payload.fan_speed,
                vane_vertical_direction=payload.vane_vertical_direction,
                vane_horizontal_direction=payload.vane_horizontal_direction,
            )
        )
    return entries


@router.get("/api/units/{unit_id}/rule")
async def get_rule(unit_id: str) -> Dict[str, Any]:
    rule = load_rules().get(unit_id) or TempRule()
    return {
        "enabled": rule.enabled,
        "cool_target": rule.cool_target,
        "heat_target": rule.heat_target,
        "boost_enabled": rule.boost_enabled,
        "boost_offset_c": rule.boost_offset_c,
    }


@router.put("/api/units/{unit_id}/rule")
async def update_rule(unit_id: str, payload: RulePayload) -> Dict[str, Any]:
    rule = TempRule(
        enabled=payload.enabled,
        cool_target=payload.cool_target,
        heat_target=payload.heat_target,
        boost_enabled=payload.boost_enabled,
        boost_offset_c=payload.boost_offset_c,
    )
    try:
        set_rule(unit_id, rule)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to save rule for %s: %s", unit_id, exc)
        raise HTTPException(status_code=500, detail=f"failed to save rule: {exc}")
    return {
        "enabled": rule.enabled,
        "cool_target": rule.cool_target,
        "heat_target": rule.heat_target,
        "boost_enabled": rule.boost_enabled,
        "boost_offset_c": rule.boost_offset_c,
    }


# ------------------------------------------------- solar-boost coordinator
# The fleet-level knobs sequencing solar boost across units (issue #562). Fleet
# -wide rather than per-unit, so the path is ``/api/hvac/...`` and not under
# ``/api/units/{id}`` — but it stays in this router because this is where HVAC
# rule/schedule config already lives. Its editor is a card on the **Energy tab**
# (next to the PV-system card from #561): boost is a solar-load-balancing
# concern, and that is the tab a user tunes solar behaviour on.
#
# Deliberately a JSON store re-read per tick, not `.env`: the `HVAC_BOOST_*`
# hysteresis knobs are frozen into `AutomationConfig` at start-up and cannot
# change without a tray restart. These can.


class BoostCoordinatorPayload(BaseModel):
    """Coordinator knobs as the editor sends them.

    Every field is optional so a card that saves one row on blur doesn't have to
    restate the others; an omitted field keeps its stored value.
    """

    settle_interval_s: Optional[int] = None
    admission_margin_w: Optional[float] = None
    hard_deficit_w: Optional[float] = None
    ordering_policy: Optional[str] = None


def _boost_coordinator_payload(config: BoostCoordinatorConfig) -> Dict[str, Any]:
    """Flatten the config plus the bounds the editor renders its inputs from."""
    return {
        "settle_interval_s": config.settle_interval_s,
        "admission_margin_w": config.admission_margin_w,
        "hard_deficit_w": config.hard_deficit_w,
        "ordering_policy": config.ordering_policy,
        # Served rather than duplicated in the frontend: the floor exists for a
        # physical reason (the solar meter's publish cadence), so the input's
        # `min` must track the backend's, not a hand-copied constant.
        "min_settle_interval_s": MIN_SETTLE_INTERVAL_S,
        "ordering_policies": list(ORDERING_POLICIES),
    }


@router.get("/api/hvac/boost-coordinator")
async def get_boost_coordinator() -> Dict[str, Any]:
    """The fleet solar-boost sequencing knobs (issue #562).

    Always 200: an absent or malformed file means "defaults", never a 500 — the
    engine has to keep running off a hand-broken config. That promise now has to
    be kept explicitly (issue #689), since the underlying store raises on an
    unreadable read rather than quietly returning empty.
    """
    return _boost_coordinator_payload(
        _decoration(load_boost_config, "config/hvac_boost.json", BoostCoordinatorConfig())
    )


@router.put("/api/hvac/boost-coordinator")
async def update_boost_coordinator(payload: BoostCoordinatorPayload) -> Dict[str, Any]:
    """Replace the coordinator knobs, writing ``config/hvac_boost.json`` atomically.

    Strict, unlike the read path: an out-of-range value is a 400 naming the field
    (notably a settle interval under the 5-minute floor), never a silent clamp.
    The engine re-reads the file on its next tick — no restart.
    """
    stored = load_boost_config()
    config = BoostCoordinatorConfig(
        settle_interval_s=(
            stored.settle_interval_s
            if payload.settle_interval_s is None
            else payload.settle_interval_s
        ),
        admission_margin_w=(
            stored.admission_margin_w
            if payload.admission_margin_w is None
            else payload.admission_margin_w
        ),
        hard_deficit_w=(
            stored.hard_deficit_w
            if payload.hard_deficit_w is None
            else payload.hard_deficit_w
        ),
        ordering_policy=(
            stored.ordering_policy
            if payload.ordering_policy is None
            else payload.ordering_policy
        ),
    )
    try:
        save_boost_config(config)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to save boost coordinator: %s", exc)
        raise HTTPException(status_code=500, detail=f"failed to save: {exc}")
    return _boost_coordinator_payload(config)


@router.get("/api/units/{unit_id}/schedule")
async def get_schedule(unit_id: str) -> Dict[str, Any]:
    entries = load_schedules().get(unit_id, [])
    first = entries[0] if entries else ScheduleEntry(enabled=False)
    summary = _schedule_summary(entries)
    return {
        **summary,
        "entries": [_entry_dict(e) for e in entries],
        # Compatibility with the old single-schedule response shape.
        "power": first.power,
        "operation_mode": first.operation_mode,
        "set_temperature": first.set_temperature,
        "fan_speed": first.fan_speed,
        "vane_vertical_direction": first.vane_vertical_direction,
        "vane_horizontal_direction": first.vane_horizontal_direction,
    }


@router.put("/api/units/{unit_id}/schedule")
async def update_schedule(unit_id: str, request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
        entries = _coerce_schedule_entries(body)
        set_schedules(unit_id, entries)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to save schedule for %s: %s", unit_id, exc)
        raise HTTPException(status_code=500, detail=f"failed to save schedule: {exc}")

    summary = _schedule_summary(entries)
    first = entries[0] if entries else ScheduleEntry(enabled=False)
    return {
        **summary,
        "entries": [_entry_dict(e) for e in entries],
        # Compatibility with the old single-schedule response shape.
        "power": first.power,
        "operation_mode": first.operation_mode,
        "set_temperature": first.set_temperature,
        "fan_speed": first.fan_speed,
        "vane_vertical_direction": first.vane_vertical_direction,
        "vane_horizontal_direction": first.vane_horizontal_direction,
    }
