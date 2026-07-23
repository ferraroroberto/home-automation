"""MAC-or-IP device pinning (issue #504).

All addresses here are synthetic — this repo is public.
"""

from __future__ import annotations

import asyncio

import pytest

from src import device_address
from src.device_address import (
    DeviceAddressError,
    is_mac,
    resolve_device_host,
    resolve_device_host_sync,
    resolve_url_host,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    device_address.clear_cache()
    yield
    device_address.clear_cache()


def _stub_lookup(monkeypatch, result, calls=None):
    async def fake(mac):
        if calls is not None:
            calls.append(mac)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(device_address, "_lookup", fake)


@pytest.mark.parametrize("value", [
    "AA:BB:CC:DD:EE:FF", "aa:bb:cc:dd:ee:ff", "AA-BB-CC-DD-EE-FF",
])
def test_is_mac_accepts_both_separators_and_either_case(value) -> None:
    assert is_mac(value)


@pytest.mark.parametrize("value", [
    "192.168.9.10",            # IPv4
    "nas.local",               # hostname
    "fe80::1",                 # IPv6 — must not be mistaken for a MAC
    "AA:BB:CC:DD:EE",          # too short
    "AA:BB:CC:DD:EE:FF:00",    # too long
    "AA:BB-CC:DD:EE:FF",       # mixed separators
    "ZZ:BB:CC:DD:EE:FF",       # non-hex
    "", None,
])
def test_is_mac_rejects_everything_that_is_not_a_mac(value) -> None:
    assert not is_mac(value)


def test_literal_host_passes_through_without_any_lookup(monkeypatch) -> None:
    """The common path must cost nothing — no config migration, no network call."""
    calls: list[str] = []
    _stub_lookup(monkeypatch, "192.168.9.99", calls)

    assert asyncio.run(resolve_device_host("192.168.9.10")) == "192.168.9.10"
    assert asyncio.run(resolve_device_host("nas.local")) == "nas.local"
    assert calls == []


def test_blank_stays_none_so_unconfigured_is_distinguishable(monkeypatch) -> None:
    _stub_lookup(monkeypatch, "192.168.9.99")
    assert asyncio.run(resolve_device_host(None)) is None
    assert asyncio.run(resolve_device_host("   ")) is None


def test_mac_resolves_to_the_current_address(monkeypatch) -> None:
    _stub_lookup(monkeypatch, "192.168.9.42")
    assert asyncio.run(resolve_device_host("AA:BB:CC:DD:EE:FF")) == "192.168.9.42"


def test_resolution_is_cached_within_the_ttl(monkeypatch) -> None:
    calls: list[str] = []
    _stub_lookup(monkeypatch, "192.168.9.42", calls)

    for _ in range(3):
        assert asyncio.run(resolve_device_host("AA:BB:CC:DD:EE:FF")) == "192.168.9.42"
    assert len(calls) == 1, "a burst of device calls must resolve once, not once each"


def test_cache_key_is_separator_and_case_insensitive(monkeypatch) -> None:
    calls: list[str] = []
    _stub_lookup(monkeypatch, "192.168.9.42", calls)

    asyncio.run(resolve_device_host("AA:BB:CC:DD:EE:FF"))
    asyncio.run(resolve_device_host("aa-bb-cc-dd-ee-ff"))
    assert len(calls) == 1


def test_expired_entry_is_refreshed(monkeypatch) -> None:
    calls: list[str] = []
    _stub_lookup(monkeypatch, "192.168.9.42", calls)
    asyncio.run(resolve_device_host("AA:BB:CC:DD:EE:FF"))

    monkeypatch.setattr(device_address, "RESOLVE_TTL_S", -1.0)
    device_address._cache["AA:BB:CC:DD:EE:FF"] = ("192.168.9.42", 0.0)  # already stale
    asyncio.run(resolve_device_host("AA:BB:CC:DD:EE:FF"))
    assert len(calls) == 2


def test_falls_back_to_last_known_good_when_the_lookup_fails(monkeypatch) -> None:
    """A resolver that hard-fails while the router is briefly unreachable would be
    strictly worse than the hardcoded IP it replaces."""
    _stub_lookup(monkeypatch, "192.168.9.42")
    assert asyncio.run(resolve_device_host("AA:BB:CC:DD:EE:FF")) == "192.168.9.42"

    device_address._cache["AA:BB:CC:DD:EE:FF"] = ("192.168.9.42", 0.0)  # expire it
    _stub_lookup(monkeypatch, None)  # inventory now unavailable
    assert asyncio.run(resolve_device_host("AA:BB:CC:DD:EE:FF")) == "192.168.9.42"


def test_unresolvable_mac_with_no_history_raises_rather_than_guessing(monkeypatch) -> None:
    """Silently connecting to whatever now holds a stale address is the failure
    mode this whole feature exists to prevent."""
    _stub_lookup(monkeypatch, None)
    with pytest.raises(DeviceAddressError) as exc:
        asyncio.run(resolve_device_host("AA:BB:CC:DD:EE:FF"))
    assert "AA:BB:CC:DD:EE:FF" in str(exc.value)


def test_a_moved_device_is_picked_up_after_the_ttl(monkeypatch) -> None:
    _stub_lookup(monkeypatch, "192.168.9.42")
    assert asyncio.run(resolve_device_host("AA:BB:CC:DD:EE:FF")) == "192.168.9.42"

    device_address._cache["AA:BB:CC:DD:EE:FF"] = ("192.168.9.42", 0.0)
    _stub_lookup(monkeypatch, "192.168.9.77")
    assert asyncio.run(resolve_device_host("AA:BB:CC:DD:EE:FF")) == "192.168.9.77"


# --- URL host substitution --------------------------------------------------- #

def test_resolve_url_host_replaces_host_and_keeps_the_port(monkeypatch) -> None:
    _stub_lookup(monkeypatch, "192.168.9.50")
    out = asyncio.run(resolve_url_host("http://192.168.9.4:8123", "AA:BB:CC:DD:EE:FF"))
    assert out == "http://192.168.9.50:8123"


def test_resolve_url_host_preserves_scheme_path_and_query(monkeypatch) -> None:
    _stub_lookup(monkeypatch, "192.168.9.50")
    out = asyncio.run(
        resolve_url_host("https://old.local/api/x?y=1", "AA:BB:CC:DD:EE:FF")
    )
    assert out == "https://192.168.9.50/api/x?y=1"


def test_resolve_url_host_is_a_noop_without_a_mac(monkeypatch) -> None:
    calls: list[str] = []
    _stub_lookup(monkeypatch, "192.168.9.50", calls)
    assert asyncio.run(resolve_url_host("http://192.168.9.4:8123", "")) == \
        "http://192.168.9.4:8123"
    assert asyncio.run(resolve_url_host("http://192.168.9.4:8123", None)) == \
        "http://192.168.9.4:8123"
    assert calls == []


def test_resolve_url_host_passes_blank_url_through(monkeypatch) -> None:
    _stub_lookup(monkeypatch, "192.168.9.50")
    assert asyncio.run(resolve_url_host("", "AA:BB:CC:DD:EE:FF")) == ""
    assert asyncio.run(resolve_url_host(None, "AA:BB:CC:DD:EE:FF")) is None


# --- sync entry point (ops scripts) ------------------------------------------ #

def test_sync_wrapper_resolves_outside_a_loop(monkeypatch) -> None:
    _stub_lookup(monkeypatch, "192.168.9.42")
    assert resolve_device_host_sync("AA:BB:CC:DD:EE:FF") == "192.168.9.42"


def test_sync_wrapper_passes_literals_through_untouched(monkeypatch) -> None:
    calls: list[str] = []
    _stub_lookup(monkeypatch, "192.168.9.42", calls)
    assert resolve_device_host_sync("192.168.9.10") == "192.168.9.10"
    assert calls == []


def test_sync_wrapper_refuses_to_deadlock_inside_a_running_loop(monkeypatch) -> None:
    """Called from async code it would deadlock, so it degrades to unresolved."""
    _stub_lookup(monkeypatch, "192.168.9.42")

    async def inner():
        return resolve_device_host_sync("AA:BB:CC:DD:EE:FF")

    assert asyncio.run(inner()) == "AA:BB:CC:DD:EE:FF"
