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


def _channel(channel, key, power_w=None, power_raw_w=None, energy_kwh=None):
    """A stand-in for :class:`src.athom_client.CircuitReading`."""
    return SimpleNamespace(
        channel=channel, key=key, power_w=power_w, power_raw_w=power_raw_w,
        current_a=None, energy_kwh=energy_kwh, inverted=power_w != power_raw_w,
    )


def _meter(meter_id, reachable, channels):
    """A stand-in for :class:`src.athom_client.MeterState`."""
    return SimpleNamespace(meter_id=meter_id, reachable=reachable, channels=channels)


def test_circuit_readings_store_corrected_and_raw_power() -> None:
    # Channel 2's clamp is fitted backwards, so invert corrects it. Both the
    # corrected and the raw figure must survive, or flipping the user setting
    # later would silently invalidate every prior row (#740).
    meter = _meter("AA:BB:CC:DD:EE:01", True, [
        _channel(1, "AA:BB:CC:DD:EE:01:1", power_w=412.5, power_raw_w=412.5, energy_kwh=9.5),
        _channel(2, "AA:BB:CC:DD:EE:01:2", power_w=180.0, power_raw_w=-180.0, energy_kwh=3.25),
    ])
    rows = A.circuit_readings([meter])
    assert {r.domain for r in rows} == {"circuit"}

    ch1 = _by_metric([r for r in rows if r.entity_id == "AA:BB:CC:DD:EE:01:1"])
    assert ch1["power_w"].value_num == 412.5 and ch1["power_w"].unit == "W"
    assert ch1["energy_kwh"].value_num == 9.5 and ch1["energy_kwh"].unit == "kWh"
    assert ch1["power_w"].quality == "ok"

    ch2 = _by_metric([r for r in rows if r.entity_id == "AA:BB:CC:DD:EE:01:2"])
    assert ch2["power_w"].value_num == 180.0
    assert ch2["power_raw_w"].value_num == -180.0


def test_circuit_readings_entity_is_the_stable_prefs_key() -> None:
    # The MAC-based "<meter_id>:<channel>" key is what makes a series survive a
    # DHCP move: the entity must be that key, never the meter's address.
    meter = _meter("AA:BB:CC:DD:EE:01", True, [_channel(3, "AA:BB:CC:DD:EE:01:3", power_w=0.0, power_raw_w=0.0)])
    assert {r.entity_id for r in A.circuit_readings([meter])} == {"AA:BB:CC:DD:EE:01:3"}


def test_circuit_readings_unreachable_meter_is_null_never_zero() -> None:
    # An unreachable meter still carries its channels (src.athom_client's
    # _unreachable). "Could not read the clamp" must stay distinguishable from
    # "the clamp read 0 W" — which is itself a real, common answer here.
    meter = _meter("AA:BB:CC:DD:EE:02", False, [_channel(1, "AA:BB:CC:DD:EE:02:1")])
    rows = A.circuit_readings([meter])
    assert rows, "an unreachable meter must still record its channels"
    for r in rows:
        assert r.value_num is None
        assert r.quality == "unreachable"


def test_circuit_readings_records_every_channel_including_unfitted() -> None:
    # A 6-channel meter on a 4-breaker board: channels 5 and 6 have no clamp and
    # never will until one is added, at which point they must just start
    # reading. Nothing may be dropped for reading nothing — and because this
    # module never imports circuit_prefs, a `hidden` channel cannot be dropped
    # either: hiding stays presentation-only by construction.
    channels = [_channel(n, f"AA:BB:CC:DD:EE:03:{n}", power_w=10.0 * n, power_raw_w=10.0 * n) for n in (1, 2, 3, 4)]
    channels += [_channel(n, f"AA:BB:CC:DD:EE:03:{n}") for n in (5, 6)]
    rows = A.circuit_readings([_meter("AA:BB:CC:DD:EE:03", True, channels)])
    assert len({r.entity_id for r in rows}) == 6
    unfitted = _by_metric([r for r in rows if r.entity_id == "AA:BB:CC:DD:EE:03:5"])
    # Reachable meter, no clamp: quality is honest ("ok" — we did read it), and
    # the absent number is still NULL rather than a fabricated 0.
    assert unfitted["power_w"].quality == "ok"
    assert unfitted["power_w"].value_num is None


def test_circuit_readings_no_meters_is_empty_not_an_error() -> None:
    assert A.circuit_readings([]) == []
