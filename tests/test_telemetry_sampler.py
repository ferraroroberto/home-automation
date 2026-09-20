"""Unit tests for :mod:`app.webapp.telemetry_sampler` — the domain registry and
its per-domain failure isolation (#740).

No network and no DB: the collectors and ``record_readings`` are replaced with
stand-ins, so nothing here can reach a meter or the real telemetry store.

**Scope note.** The isolation tests below are *guards*, not proof of a fix: the
per-domain ``try/except`` in ``_sample_once`` predates this change and these
tests pass against pre-#740 code too. They are here because #740 adds a domain
that talks to hardware over mDNS — the flakiest source in the sampler — and the
"one bad meter must not drop the other domains" property had no test at all
before. The genuinely new-behaviour coverage lives in
``tests/test_telemetry_adapters.py``.
"""

from __future__ import annotations

import asyncio

import pytest

from app.webapp import telemetry_sampler as ts
from src import telemetry


@pytest.fixture
def recorded(monkeypatch):
    """Capture what the sampler would have written, touching no database."""
    rows: list = []

    def _record(batch):
        rows.extend(batch)
        return len(batch)

    monkeypatch.setattr(telemetry, "record_readings", _record)
    return rows


def _collector(rows):
    async def _run():
        return list(rows)

    return _run


def _failing(message="meter read blew up"):
    async def _run():
        raise RuntimeError(message)

    return _run


def test_every_collector_key_has_a_matching_config_gate() -> None:
    """``_enabled_domains`` does ``getattr(config, name)``, so a registry key
    that doesn't match a ``TelemetrySamplerConfig`` field would make the domain
    silently unreachable (or raise) instead of sampling."""
    config = ts.TelemetrySamplerConfig()
    for name in ts._COLLECTORS:
        assert hasattr(config, name), f"_COLLECTORS key {name!r} has no config gate"


def test_circuits_is_registered_and_on_by_default() -> None:
    assert "circuits" in ts._COLLECTORS
    assert "circuits" in ts._enabled_domains(ts.TelemetrySamplerConfig())


def test_circuits_gate_turns_only_circuits_off(monkeypatch) -> None:
    # load_dotenv is stubbed so the test reads the environment it set, not
    # whatever .env happens to be on the box running it.
    monkeypatch.setattr(ts, "load_dotenv", lambda **kwargs: None)
    monkeypatch.setenv("TELEMETRY_SAMPLE_CIRCUITS", "false")
    config = ts.load_sampler_config()
    assert config.circuits is False
    assert config.hvac is True and config.plugs is True
    assert "circuits" not in ts._enabled_domains(config)


def test_a_failing_domain_does_not_drop_the_others(monkeypatch, recorded) -> None:
    """A meter read that raises must cost only its own domain's rows."""
    monkeypatch.setattr(ts, "_COLLECTORS", {
        "circuits": _failing(),
        "hvac": _collector(["hvac-a", "hvac-b"]),
        "plugs": _collector(["plug-a"]),
    })
    written = asyncio.run(ts._sample_once(ts.TelemetrySamplerConfig()))
    assert written == 3
    assert recorded == ["hvac-a", "hvac-b", "plug-a"]


def test_a_failing_domain_does_not_kill_the_loop(monkeypatch, recorded) -> None:
    """``_sample_once`` never raises, so the sampler ticks again next interval —
    the property that keeps one dead meter from ending all collection."""
    monkeypatch.setattr(ts, "_COLLECTORS", {"circuits": _failing(), "hvac": _failing()})
    assert asyncio.run(ts._sample_once(ts.TelemetrySamplerConfig())) == 0
    assert recorded == []
    # And a later tick, once the meter is back, still records normally.
    monkeypatch.setattr(ts, "_COLLECTORS", {"circuits": _collector(["circuit-a"])})
    assert asyncio.run(ts._sample_once(ts.TelemetrySamplerConfig())) == 1
    assert recorded == ["circuit-a"]


def test_a_failing_write_is_isolated_too(monkeypatch, recorded) -> None:
    """The store itself failing (locked DB, full disk) must not drop the rest."""
    def _record(batch):
        if batch and batch[0].startswith("circuit"):
            raise RuntimeError("database is locked")
        recorded.extend(batch)
        return len(batch)

    monkeypatch.setattr(telemetry, "record_readings", _record)
    monkeypatch.setattr(ts, "_COLLECTORS", {
        "circuits": _collector(["circuit-a"]),
        "hvac": _collector(["hvac-a"]),
    })
    assert asyncio.run(ts._sample_once(ts.TelemetrySamplerConfig())) == 1
    assert recorded == ["hvac-a"]
