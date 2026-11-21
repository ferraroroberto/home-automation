"""The HA integration's shared coordinator is created once per key.

HA sets platforms up concurrently and two of them (alarm panel, zone sensors)
ask for ``"security"``. The coordinator is registered only after its first
refresh, so without a per-key lock both miss the key and two pollers run.
``homeassistant`` is not installed here, so the few names the module imports
are stubbed and the real ``coordinator.py`` is loaded from the asset folder.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

_PKG_DIR = Path(__file__).resolve().parents[1] / "assets" / "home_assistant" / "home_automation_app"
_PKG = "ha_app_under_test"


class _FakeCoordinator:
    instances: list["_FakeCoordinator"] = []

    def __init__(self, hass, logger, *, name, update_method, update_interval) -> None:
        self.name = name
        self.refreshes = 0
        _FakeCoordinator.instances.append(self)

    async def async_refresh(self) -> None:
        self.refreshes += 1
        await asyncio.sleep(0.01)  # the first refresh is a network call: others get to run


def _load_coordinator(monkeypatch: pytest.MonkeyPatch):
    stubs = {
        "homeassistant": types.ModuleType("homeassistant"),
        "homeassistant.core": types.ModuleType("homeassistant.core"),
        "homeassistant.helpers": types.ModuleType("homeassistant.helpers"),
        "homeassistant.helpers.update_coordinator": types.ModuleType(
            "homeassistant.helpers.update_coordinator"
        ),
    }
    stubs["homeassistant.core"].HomeAssistant = object
    uc = stubs["homeassistant.helpers.update_coordinator"]
    uc.DataUpdateCoordinator = _FakeCoordinator
    uc.UpdateFailed = type("UpdateFailed", (Exception,), {})
    for name, module in stubs.items():
        monkeypatch.setitem(sys.modules, name, module)

    pkg = types.ModuleType(_PKG)
    pkg.__path__ = [str(_PKG_DIR)]
    pkg.get_scan_interval = lambda hass: None
    monkeypatch.setitem(sys.modules, _PKG, pkg)

    spec = importlib.util.spec_from_file_location(f"{_PKG}.coordinator", _PKG_DIR / "coordinator.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_concurrent_platform_setups_share_one_coordinator(monkeypatch) -> None:
    module = _load_coordinator(monkeypatch)
    _FakeCoordinator.instances.clear()
    hass = types.SimpleNamespace(data={module.DOMAIN: {}})

    async def fetcher():
        return {}

    async def run():
        return await asyncio.gather(
            module.async_get_coordinator(hass, "security", fetcher),
            module.async_get_coordinator(hass, "security", fetcher),
        )

    first, second = asyncio.run(run())

    assert first is second
    assert len(_FakeCoordinator.instances) == 1
    assert first.refreshes == 1


def test_different_keys_get_their_own_coordinator(monkeypatch) -> None:
    module = _load_coordinator(monkeypatch)
    _FakeCoordinator.instances.clear()
    hass = types.SimpleNamespace(data={module.DOMAIN: {}})

    async def fetcher():
        return {}

    async def run():
        return await asyncio.gather(
            module.async_get_coordinator(hass, "security", fetcher),
            module.async_get_coordinator(hass, "energy", fetcher),
        )

    security, energy = asyncio.run(run())

    assert security is not energy
    assert len(_FakeCoordinator.instances) == 2
