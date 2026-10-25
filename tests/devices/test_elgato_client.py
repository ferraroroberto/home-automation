"""Pure Elgato client helpers: no LAN discovery or real lights."""

from __future__ import annotations

import asyncio

import pytest

import src.elgato_client as elgato
from src.elgato_client import (
    ElgatoCommandError,
    ElgatoEndpoint,
    ElgatoLight,
    kelvin_to_temperature,
    set_light_state,
    temperature_to_kelvin,
    _clamp_int,
    _parse_host,
)


def test_temperature_conversion_round_trips_common_values() -> None:
    assert temperature_to_kelvin(200) == 5000
    assert kelvin_to_temperature(4000) == 250


def test_parse_host_accepts_plain_host_port_and_url() -> None:
    plain = _parse_host("192.0.2.10")
    assert plain is not None
    assert plain.host == "192.0.2.10"
    assert plain.port == 9123

    explicit = _parse_host("http://light.local:9124/elgato/lights")
    assert explicit is not None
    assert explicit.host == "light.local"
    assert explicit.port == 9124


def test_clamp_int_rejects_out_of_range_values() -> None:
    assert _clamp_int(42, 3, 100, "brightness") == 42
    with pytest.raises(ElgatoCommandError, match="brightness"):
        _clamp_int(1, 3, 100, "brightness")


def test_brightness_write_omits_unsupported_temperature(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = ElgatoEndpoint(host="192.0.2.10")
    current = ElgatoLight(
        light_id=endpoint.light_id,
        host=endpoint.host,
        port=endpoint.port,
        name="Fixture Strip",
        product_name="Elgato Light Strip Pro",
        firmware="152",
        on=False,
        brightness=50,
        temperature=0,
        temperature_k=0,
        supports_temperature=False,
    )
    captured = {}

    async def fake_discover_endpoints() -> list[ElgatoEndpoint]:
        return [endpoint]

    async def fake_read_light(_endpoint: ElgatoEndpoint) -> ElgatoLight:
        return current

    async def fake_put_json(_session, _endpoint, _path, payload) -> None:
        captured["payload"] = payload

    monkeypatch.setattr(elgato, "discover_endpoints", fake_discover_endpoints)
    monkeypatch.setattr(elgato, "read_light", fake_read_light)
    monkeypatch.setattr(elgato, "_put_json", fake_put_json)

    asyncio.run(set_light_state(endpoint.light_id, brightness=60))

    assert captured["payload"]["lights"] == [{"on": 0, "brightness": 60}]
