"""Per-domain reading adapters for the unified telemetry store (#290).

Each function maps one domain's live reading object(s) into a flat list of
:class:`src.telemetry.Reading` rows — the narrow ``(entity, metric, value)``
shape the store records. Pure and UI-free: no network, no DB, no clock, so they
unit-test directly against the dataclasses the device clients already return.

Conventions held across every adapter:

* Numeric metrics use ``value_num``; categorical ones (mode, on/off, status)
  use ``value_txt``. An **absent numeric reading stays ``None``** (asleep ≠ 0).
* ``quality`` is the normalized reachability — ``"ok"`` when the device
  answered, ``"unreachable"`` when it didn't — so every domain's differently
  named reachability flag collapses to one queryable notion.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from src.telemetry import Reading


def _onoff(value: Optional[bool]) -> Optional[str]:
    """Render a tri-state boolean as ``on``/``off``/``None``."""
    if value is None:
        return None
    return "on" if value else "off"


def hvac_readings(devices: Iterable[Any]) -> List[Reading]:
    """Map MELCloud ``DeviceInfo`` units → readings (temps, mode, fan, power).

    MELCloud Home exposes no watts/compressor state, so this is temperature +
    mode telemetry only. A unit with no room reading is treated as unreachable.
    """
    rows: List[Reading] = []
    for d in devices:
        quality = "ok" if d.room_temperature is not None else "unreachable"
        rows.append(Reading("hvac", d.unit_id, "room_temperature", value_num=d.room_temperature, unit="degC", quality=quality))
        rows.append(Reading("hvac", d.unit_id, "set_temperature", value_num=d.set_temperature, unit="degC", quality=quality))
        rows.append(Reading("hvac", d.unit_id, "power", value_txt=_onoff(d.power), quality=quality))
        rows.append(Reading("hvac", d.unit_id, "operation_mode", value_txt=d.operation_mode, quality=quality))
        rows.append(Reading("hvac", d.unit_id, "fan_speed", value_txt=d.fan_speed, quality=quality))
    return rows


def plug_readings(states: Iterable[Dict[str, Any]]) -> List[Reading]:
    """Map Tuya ``read_device_state`` dicts → plug readings (power/volts/amps/kWh).

    The plug is one of the few domains exposing real watts. Each dict carries
    ``device_id`` + ``reachable`` plus the metered values (any of which may be
    ``None`` on a non-metered or partly-reporting plug).
    """
    rows: List[Reading] = []
    for s in states:
        entity = s.get("device_id")
        if not entity:
            continue
        quality = "ok" if s.get("reachable") else "unreachable"
        rows.append(Reading("plug", entity, "switch_on", value_txt=_onoff(s.get("switch_on")), quality=quality))
        rows.append(Reading("plug", entity, "power_w", value_num=s.get("power_w"), unit="W", quality=quality))
        rows.append(Reading("plug", entity, "voltage_v", value_num=s.get("voltage_v"), unit="V", quality=quality))
        rows.append(Reading("plug", entity, "current_ma", value_num=s.get("current_ma"), unit="mA", quality=quality))
        rows.append(Reading("plug", entity, "energy_kwh", value_num=s.get("energy_kwh"), unit="kWh", quality=quality))
    return rows


def ups_readings(state: Any, entity_id: str = "ups") -> List[Reading]:
    """Map a ``UpsState`` → UPS readings (charge, runtime, load, voltages)."""
    quality = "ok" if state.available else "unreachable"
    return [
        Reading("ups", entity_id, "battery_charge_pct", value_num=state.battery_charge_pct, unit="%", quality=quality),
        Reading("ups", entity_id, "runtime_seconds", value_num=state.runtime_seconds, unit="s", quality=quality),
        Reading("ups", entity_id, "load_w", value_num=state.load_w, unit="W", quality=quality),
        Reading("ups", entity_id, "load_pct", value_num=state.load_pct, unit="%", quality=quality),
        Reading("ups", entity_id, "input_voltage_v", value_num=state.input_voltage_v, unit="V", quality=quality),
        Reading("ups", entity_id, "battery_voltage_v", value_num=state.battery_voltage_v, unit="V", quality=quality),
        Reading("ups", entity_id, "mains_online", value_txt=_onoff(state.mains_online), quality=quality),
        Reading("ups", entity_id, "status", value_txt=state.status, quality=quality),
    ]


def light_readings(lights: Iterable[Any]) -> List[Reading]:
    """Map Elgato ``ElgatoLight`` objects → light readings (on, brightness, K)."""
    rows: List[Reading] = []
    for light in lights:
        quality = "ok" if light.reachable else "unreachable"
        rows.append(Reading("light", light.light_id, "on", value_txt=_onoff(light.on), quality=quality))
        rows.append(Reading("light", light.light_id, "brightness", value_num=light.brightness, unit="%", quality=quality))
        if light.supports_temperature:
            rows.append(Reading("light", light.light_id, "temperature_k", value_num=light.temperature_k, unit="K", quality=quality))
    return rows


def presence_readings(people: Iterable[Dict[str, Any]]) -> List[Reading]:
    """Map person presence dicts → a minimal ``at_home`` reading per person.

    Presence is primarily captured as *events* (transitions, #289); this logs a
    coarse periodic 1/0 state for trend context. Each dict needs ``entity_id``
    and ``at_home`` (bool); other keys are ignored.
    """
    rows: List[Reading] = []
    for p in people:
        entity = p.get("entity_id")
        if not entity:
            continue
        at_home = p.get("at_home")
        rows.append(
            Reading(
                "presence",
                entity,
                "at_home",
                value_num=(1.0 if at_home else 0.0) if at_home is not None else None,
                quality="ok",
            )
        )
    return rows
