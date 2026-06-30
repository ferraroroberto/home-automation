"""Unit tests for :mod:`src.telemetry_adapters` — pure reading mappers (#290).

No network, no DB: feed the adapters lightweight stand-ins shaped like the real
device dataclasses/dicts and assert the flat ``Reading`` rows they emit.
"""

from __future__ import annotations

from types import SimpleNamespace

from src import telemetry_adapters as A


def _by_metric(rows):
    return {r.metric: r for r in rows}


def test_hvac_readings_maps_temps_mode_and_power() -> None:
    unit = SimpleNamespace(
        unit_id="u1", name="Bedroom", power=True, operation_mode="heat",
        room_temperature=20.5, set_temperature=22.0, fan_speed="auto",
    )
    rows = A.hvac_readings([unit])
    m = _by_metric(rows)
    assert m["room_temperature"].value_num == 20.5 and m["room_temperature"].unit == "degC"
    assert m["set_temperature"].value_num == 22.0
    assert m["power"].value_txt == "on"
    assert m["operation_mode"].value_txt == "heat"
    assert all(r.domain == "hvac" and r.entity_id == "u1" for r in rows)


def test_hvac_missing_room_temp_marks_unreachable_and_null() -> None:
    unit = SimpleNamespace(
        unit_id="u1", name="X", power=None, operation_mode=None,
        room_temperature=None, set_temperature=None, fan_speed=None,
    )
    m = _by_metric(A.hvac_readings([unit]))
    # Asleep/offline: numeric stays None, never 0, and quality is unreachable.
    assert m["room_temperature"].value_num is None
    assert m["room_temperature"].quality == "unreachable"
    assert m["power"].value_txt is None  # None bool -> None, not "off"


def test_plug_readings_power_and_reachability() -> None:
    states = [
        {"device_id": "d1", "reachable": True, "switch_on": True, "power_w": 42.0,
         "voltage_v": 230.0, "current_ma": 180.0, "energy_kwh": 1.5},
        {"device_id": "d2", "reachable": False},
    ]
    rows = A.plug_readings(states)
    d1 = _by_metric([r for r in rows if r.entity_id == "d1"])
    assert d1["power_w"].value_num == 42.0 and d1["power_w"].unit == "W"
    assert d1["switch_on"].value_txt == "on"
    d2 = _by_metric([r for r in rows if r.entity_id == "d2"])
    assert d2["power_w"].value_num is None and d2["power_w"].quality == "unreachable"


def test_plug_readings_skips_entry_without_id() -> None:
    assert A.plug_readings([{"reachable": True}]) == []


def test_ups_readings_core_metrics() -> None:
    state = SimpleNamespace(
        available=True, status="online", mains_online=True, battery_charge_pct=100.0,
        runtime_seconds=1800, load_w=120.0, load_pct=12.0, input_voltage_v=230.0,
        battery_voltage_v=27.0,
    )
    m = _by_metric(A.ups_readings(state))
    assert m["battery_charge_pct"].value_num == 100.0
    assert m["load_w"].value_num == 120.0 and m["load_w"].unit == "W"
    assert m["mains_online"].value_txt == "on"
    assert m["status"].value_txt == "online"


def test_light_readings_includes_temperature_only_when_supported() -> None:
    warm = SimpleNamespace(light_id="l1", on=True, brightness=80, temperature_k=3000, supports_temperature=True, reachable=True)
    plain = SimpleNamespace(light_id="l2", on=False, brightness=0, temperature_k=0, supports_temperature=False, reachable=True)
    rows = A.light_readings([warm, plain])
    metrics_l1 = {r.metric for r in rows if r.entity_id == "l1"}
    metrics_l2 = {r.metric for r in rows if r.entity_id == "l2"}
    assert "temperature_k" in metrics_l1
    assert "temperature_k" not in metrics_l2
    assert _by_metric([r for r in rows if r.entity_id == "l1"])["on"].value_txt == "on"


def test_presence_readings_one_to_zero() -> None:
    rows = A.presence_readings([
        {"entity_id": "alice", "at_home": True},
        {"entity_id": "bob", "at_home": False},
        {"no_id": True},
    ])
    m = {r.entity_id: r for r in rows}
    assert len(rows) == 2  # the id-less entry is skipped
    assert m["alice"].value_num == 1.0
    assert m["bob"].value_num == 0.0
