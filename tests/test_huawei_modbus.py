"""Tests for the local Modbus energy path (issue #618).

Nothing here touches the real inverter: the transport
(:func:`src.huawei_modbus._read_blocks`) and the config loader are both
monkeypatched, so the register decoding, the sign convention, the caching, the
MAC rediscovery and the fall-back-to-cloud branch are all exercised offline.

The sign tests are the load-bearing ones. Register 37113 is positive when
*exporting* — the opposite of the cloud's ``meterActivePower`` — and this repo
has already shipped that inversion backwards once.
"""

from __future__ import annotations

import asyncio
import struct

import pytest

from src import huawei_client, huawei_modbus
from src.huawei_modbus import (
    ModbusConfig,
    _i32,
    _state_from_registers,
    fetch_modbus_state,
)


@pytest.fixture(autouse=True)
def _reset():
    """Cache, backoff and rediscovered host are module state — don't leak them."""
    huawei_modbus.reset_state()
    yield
    huawei_modbus.reset_state()


def _regs(value: int) -> tuple[int, int]:
    return struct.unpack(">HH", struct.pack(">i", value))


def _inverter_block(active_w: int) -> list[int]:
    """An inverter block (32064..32081) carrying ``active_w`` at 32080."""
    block = [0] * huawei_modbus._INVERTER_COUNT
    block[16], block[17] = _regs(active_w)
    return block


def _meter_block(power_w: int, status: int = 1) -> list[int]:
    """A meter block (37100..37114) carrying ``power_w`` at 37113."""
    block = [0] * huawei_modbus._METER_COUNT
    block[0] = status
    block[13], block[14] = _regs(power_w)
    return block


def _config(**overrides) -> ModbusConfig:
    base = dict(
        mac="ec:55:1c:7f:c5:dc",
        host="192.168.0.108",
        port=502,
        unit_id=1,
        timeout_s=3.0,
        cache_ttl_s=5,
    )
    base.update(overrides)
    return ModbusConfig(**base)


def _use_config(monkeypatch, config: ModbusConfig) -> None:
    """Pin the config, bypassing ``.env`` entirely.

    ``_load_config`` calls ``load_dotenv(override=True)``, so a real ``.env`` on
    the dev box would win over ``monkeypatch.setenv`` and point the tests at the
    actual dongle. Replacing the loader is the only isolation that holds.
    """
    monkeypatch.setattr(huawei_modbus, "_load_config", lambda: config)


# --------------------------------------------------------------- decoding

def test_i32_decodes_negative_registers():
    """A signed int32 spanning two registers — the grid meter goes negative."""
    assert _i32(_regs(-1234), 0) == -1234
    assert _i32(_regs(3475), 0) == 3475


# ---------------------------------------------------------- sign convention

def test_positive_37113_is_exporting():
    """+1015 W at 37113 means power is leaving the house, not entering it.

    Proven against the live system on 2026-08-04: the cloud reported exporting
    1004 W while this register read +1015 W.
    """
    state = _state_from_registers(_inverter_block(3419), _meter_block(1015))

    assert state.grid_export_w == 1015.0
    assert state.grid_import_w == 0.0
    # Surplus is positive when exporting, and the house takes what the inverter
    # produced minus what went to the grid.
    assert state.pv_surplus_w == 1015.0
    assert state.pv_power_w == 3419.0
    assert state.house_consumption_w == 2404.0
    assert state.meter_reachable is True
    assert state.inverter_reachable is True


def test_negative_37113_is_importing():
    """The after-dark case: no PV, the house still draws, so the meter is negative."""
    state = _state_from_registers(_inverter_block(0), _meter_block(-2300))

    assert state.grid_import_w == 2300.0
    assert state.grid_export_w == 0.0
    assert state.pv_surplus_w == -2300.0
    assert state.house_consumption_w == 2300.0


def test_sign_is_the_opposite_of_the_cloud():
    """Guard against the two sources ever being collapsed into one split.

    The same physical situation — exporting 1 kW — is a *negative*
    ``meterActivePower`` in the cloud payload and a *positive* 37113 locally.
    """
    local = _state_from_registers(_inverter_block(3000), _meter_block(1000))
    cloud = huawei_client._state_from_point(
        {"productPower": 3.0, "usePower": 2.0, "meterActivePower": -1.0}
    )

    assert local.grid_export_w == cloud.grid_export_w == 1000.0
    assert local.grid_import_w == cloud.grid_import_w == 0.0


def test_offline_power_sensor_keeps_pv_but_drops_the_grid():
    """A meter that isn't in the normal state says nothing about the inverter."""
    state = _state_from_registers(_inverter_block(2500), _meter_block(900, status=0))

    assert state.meter_reachable is False
    assert state.grid_import_w is None
    assert state.grid_export_w is None
    assert state.pv_surplus_w is None
    assert state.inverter_reachable is True
    assert state.pv_power_w == 2500.0


# ------------------------------------------------------------ fetch + cache

def test_disabled_without_a_mac_or_host(monkeypatch):
    """No config is not an error — it is how CI and dongle-less boxes stay on cloud."""
    _use_config(monkeypatch, _config(mac=None, host=None))

    def _boom(*args, **kwargs):  # pragma: no cover — must never run
        raise AssertionError("the transport was opened despite no configuration")

    monkeypatch.setattr(huawei_modbus, "_read_blocks", _boom)
    assert asyncio.run(fetch_modbus_state()) is None


def test_reads_once_and_serves_the_rest_from_cache(monkeypatch):
    """The dongle tolerates one client; concurrent callers must not each open one."""
    _use_config(monkeypatch, _config())
    calls = []

    def _read(config, host):
        calls.append(host)
        return _inverter_block(3400), _meter_block(1100)

    monkeypatch.setattr(huawei_modbus, "_read_blocks", _read)

    async def _drive():
        first = await fetch_modbus_state()
        rest = await asyncio.gather(*(fetch_modbus_state() for _ in range(5)))
        return first, rest

    first, rest = asyncio.run(_drive())

    assert calls == ["192.168.0.108"]
    assert first.grid_export_w == 1100.0
    assert all(state is first for state in rest)


def test_transport_failure_returns_none_and_backs_off(monkeypatch):
    """An unreadable dongle is reported as ``None``, never as an exception."""
    _use_config(monkeypatch, _config(mac=None))  # no MAC → no rediscovery retry
    calls = []

    def _read(config, host):
        calls.append(host)
        raise ConnectionError("dongle rebooting")

    monkeypatch.setattr(huawei_modbus, "_read_blocks", _read)

    assert asyncio.run(fetch_modbus_state()) is None
    # Backing off: a second call must not pay another connect timeout.
    assert asyncio.run(fetch_modbus_state()) is None
    assert calls == ["192.168.0.108"]


def test_rediscovers_by_mac_when_the_lease_moved(monkeypatch):
    """The dongle drifts across DHCP leases — the MAC is the real address."""
    _use_config(monkeypatch, _config(host="192.168.0.88"))
    calls = []

    def _read(config, host):
        calls.append(host)
        if host != "192.168.0.108":
            raise ConnectionError("no route")
        return _inverter_block(3200), _meter_block(-450)

    async def _resolve(mac):
        return "192.168.0.108"

    monkeypatch.setattr(huawei_modbus, "_read_blocks", _read)
    monkeypatch.setattr("src.network_client.resolve_ip_by_mac", _resolve)

    state = asyncio.run(fetch_modbus_state())

    assert calls == ["192.168.0.88", "192.168.0.108"]
    assert state is not None
    assert state.grid_import_w == 450.0
    # The recovered address sticks for the process, so the stale hint is not
    # retried on every read.
    assert huawei_modbus._runtime_host == "192.168.0.108"


# ------------------------------------------- integration with fetch_energy_state

def test_fetch_energy_state_prefers_modbus(monkeypatch):
    """Modbus serves the flow; the cloud only supplies today's kWh counters."""
    _use_config(monkeypatch, _config())
    monkeypatch.setattr(
        huawei_modbus,
        "_read_blocks",
        lambda config, host: (_inverter_block(3419), _meter_block(1015)),
    )

    async def _stats(config):
        return {"totalBuyPower": 12.42, "totalOnGridPower": 15.34}

    monkeypatch.setattr(huawei_client, "_fetch_stats", _stats)

    state = asyncio.run(huawei_client.fetch_energy_state())

    assert state.grid_export_w == 1015.0
    assert state.pv_power_w == 3419.0
    assert state.house_consumption_w == 2404.0
    # Lifetime meter registers are deliberately not used for these: they stay
    # today's totals, from the portal.
    assert state.grid_import_kwh == 12.42
    assert state.grid_export_kwh == 15.34
    assert state.as_of is not None


def test_fetch_energy_state_falls_back_to_the_cloud(monkeypatch):
    """A dead dongle must degrade to the portal, not to an empty tile."""
    _use_config(monkeypatch, _config(mac=None))

    def _read(config, host):
        raise ConnectionError("dongle unreachable")

    monkeypatch.setattr(huawei_modbus, "_read_blocks", _read)

    async def _stats(config):
        return {
            "xAxis": ["2026-08-04 18:10"],
            "productPower": [3.302],
            "usePower": [2.298],
            "meterActivePower": [-1.004],
            "existMeter": True,
            "existInverter": True,
            "totalBuyPower": 12.42,
            "totalOnGridPower": 15.34,
        }

    monkeypatch.setattr(huawei_client, "_fetch_stats", _stats)
    monkeypatch.setattr(huawei_client, "_is_stale", lambda *a, **k: False)

    state = asyncio.run(huawei_client.fetch_energy_state())

    # Same physical situation as the Modbus test above, read the cloud's way.
    assert state.grid_export_w == 1004.0
    assert state.grid_import_w == 0.0
    assert state.pv_power_w == 3302.0


def test_cloud_counter_failure_does_not_break_the_local_read(monkeypatch):
    """The portal is optional on the Modbus path — its absence costs two fields."""
    _use_config(monkeypatch, _config())
    monkeypatch.setattr(
        huawei_modbus,
        "_read_blocks",
        lambda config, host: (_inverter_block(3419), _meter_block(1015)),
    )

    async def _stats(config):
        return None

    monkeypatch.setattr(huawei_client, "_fetch_stats", _stats)

    state = asyncio.run(huawei_client.fetch_energy_state())

    assert state.grid_export_w == 1015.0
    assert state.grid_import_kwh is None
    assert state.grid_export_kwh is None
