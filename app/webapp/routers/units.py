"""Unit read + control API over the MELCloud Home core.

``GET /api/units`` returns every air-to-air unit's live state; ``POST
/api/units/{id}`` writes the changed controls and returns the read-back
snapshot so the client can re-render just that card.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.display_names import load_display_names, set_display_name
from src.hvac_automation import (
    Schedule,
    TempRule,
    load_rules,
    load_schedules,
    set_rule,
    set_schedule,
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


def _unit_dict(
    d: DeviceInfo,
    display_names: Optional[Dict[str, str]] = None,
    rules: Optional[Dict[str, TempRule]] = None,
    schedules: Optional[Dict[str, Schedule]] = None,
) -> Dict[str, Any]:
    """Flatten a :class:`DeviceInfo` into a JSON-serialisable dict."""
    overrides = display_names or {}
    rule = (rules or {}).get(d.unit_id)
    schedule = (schedules or {}).get(d.unit_id)
    rule_target = target_for_mode(rule, d.operation_mode) if rule is not None else None
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
        },
        "schedule": {
            "enabled": bool(schedule and schedule.enabled),
            "time": schedule.time if schedule and schedule.enabled else None,
        },
    }


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
    rules = load_rules()
    schedules = load_schedules()
    return {"units": [_unit_dict(d, display_names, rules, schedules) for d in devices]}


class DisplayNamePayload(BaseModel):
    display_name: str


@router.put("/api/units/{unit_id}/display_name")
async def update_display_name(unit_id: str, payload: DisplayNamePayload) -> Dict[str, Any]:
    try:
        set_display_name(unit_id, payload.display_name.strip())
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to save display name for %s: %s", unit_id, exc)
        raise HTTPException(status_code=500, detail=f"failed to save display name: {exc}")
    return {"unit_id": unit_id, "display_name": payload.display_name.strip() or None}


@router.post("/api/units/{unit_id}")
async def control_unit(unit_id: str, request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="expected a JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")

    # Only forward the fields the client actually sent; everything else
    # stays None and is left untouched by the client.
    allowed = (
        "power",
        "operation_mode",
        "set_temperature",
        "fan_speed",
        "vane_vertical_direction",
        "vane_horizontal_direction",
    )
    kwargs = {k: body[k] for k in allowed if k in body}

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
    rules = load_rules()
    schedules = load_schedules()
    return _unit_dict(updated, display_names, rules, schedules)


class RulePayload(BaseModel):
    """Dynamic-setpoint rule for one unit (per-mode desired room temps)."""

    enabled: bool = False
    cool_target: Optional[float] = None
    heat_target: Optional[float] = None


class SchedulePayload(BaseModel):
    """Daily settings profile applied at a local ``HH:MM`` for one unit."""

    enabled: bool = False
    time: str = "08:00"
    power: bool = True
    operation_mode: Optional[str] = None
    set_temperature: Optional[float] = None
    fan_speed: Optional[str] = None
    vane_vertical_direction: Optional[str] = None
    vane_horizontal_direction: Optional[str] = None


@router.get("/api/units/{unit_id}/rule")
async def get_rule(unit_id: str) -> Dict[str, Any]:
    rule = load_rules().get(unit_id) or TempRule()
    return {
        "enabled": rule.enabled,
        "cool_target": rule.cool_target,
        "heat_target": rule.heat_target,
    }


@router.put("/api/units/{unit_id}/rule")
async def update_rule(unit_id: str, payload: RulePayload) -> Dict[str, Any]:
    rule = TempRule(
        enabled=payload.enabled,
        cool_target=payload.cool_target,
        heat_target=payload.heat_target,
    )
    try:
        set_rule(unit_id, rule)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to save rule for %s: %s", unit_id, exc)
        raise HTTPException(status_code=500, detail=f"failed to save rule: {exc}")
    return {"enabled": rule.enabled, "cool_target": rule.cool_target, "heat_target": rule.heat_target}


@router.get("/api/units/{unit_id}/schedule")
async def get_schedule(unit_id: str) -> Dict[str, Any]:
    sched = load_schedules().get(unit_id) or Schedule()
    return {
        "enabled": sched.enabled,
        "time": sched.time,
        "power": sched.power,
        "operation_mode": sched.operation_mode,
        "set_temperature": sched.set_temperature,
        "fan_speed": sched.fan_speed,
        "vane_vertical_direction": sched.vane_vertical_direction,
        "vane_horizontal_direction": sched.vane_horizontal_direction,
    }


@router.put("/api/units/{unit_id}/schedule")
async def update_schedule(unit_id: str, payload: SchedulePayload) -> Dict[str, Any]:
    sched = Schedule(
        enabled=payload.enabled,
        time=payload.time,
        power=payload.power,
        operation_mode=payload.operation_mode,
        set_temperature=payload.set_temperature,
        fan_speed=payload.fan_speed,
        vane_vertical_direction=payload.vane_vertical_direction,
        vane_horizontal_direction=payload.vane_horizontal_direction,
    )
    try:
        set_schedule(unit_id, sched)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to save schedule for %s: %s", unit_id, exc)
        raise HTTPException(status_code=500, detail=f"failed to save schedule: {exc}")
    return {
        "enabled": sched.enabled,
        "time": sched.time,
        "power": sched.power,
        "operation_mode": sched.operation_mode,
        "set_temperature": sched.set_temperature,
        "fan_speed": sched.fan_speed,
        "vane_vertical_direction": sched.vane_vertical_direction,
        "vane_horizontal_direction": sched.vane_horizontal_direction,
    }
