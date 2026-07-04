"""Behavioral tests for the alarm-override auto-bypass automation (issue #341).

Drives ``_run_event_scan`` directly (bypassing the tick-throttle that
``consider_security_read`` applies) against a fake RISCO event log, proving:
a repeated "triggered" event for an overridden zone is counted across scans and
only bypasses the detector once the configured ``max_retries`` is reached, and
the next arming event restores it and resets the session — without ever
touching a real RISCO connection or the real on-disk config.
"""

from __future__ import annotations

import asyncio

import app.webapp.security_override_automation as engine
import src.security_override as override_cfg
import src.security_override_session as session_cfg
from src.security_override import set_overrides


class _FakeEvent:
    def __init__(self, time, name=None, type=None, zone_id=None):
        self.time = time
        self.name = name
        self.type = type
        self.zone_id = zone_id


class _FakeState:
    zones = []


def _async_return(value):
    async def _f(*args, **kwargs):
        return value

    return _f


def test_override_bypasses_after_max_retries_and_restores_on_rearm(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(override_cfg, "OVERRIDES_PATH", tmp_path / "security_override.json")
    monkeypatch.setattr(session_cfg, "SESSION_PATH", tmp_path / "security_override_session.json")
    set_overrides([{"id": "jardin", "zone_id": 12, "max_retries": 2}], path=override_cfg.OVERRIDES_PATH)

    bypass_calls: list[tuple[int, bool]] = []

    async def fake_bypass(zone_id: int, bypass: bool):
        bypass_calls.append((zone_id, bypass))
        return _FakeState()

    monkeypatch.setattr(engine, "set_zone_bypass", fake_bypass)

    telemetry_calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        engine.telemetry, "record_event", lambda *a, **kw: telemetry_calls.append((a, kw))
    )

    config = engine.OverrideAutomationConfig(enabled=True, event_scan_interval_s=20)
    events: list[_FakeEvent] = [_FakeEvent("2026-07-04T10:00:00Z", name="Full Set - 'USER 1 MASTER', WEB", type="armed")]

    # First scan ever: no cursor yet -> establishes the baseline, no replay.
    monkeypatch.setattr(engine, "fetch_events", _async_return(events))
    asyncio.run(engine._run_event_scan(config))
    assert bypass_calls == []

    # First "Alarm" trigger for zone 12 -> count 1, still below max_retries=2.
    events = events + [
        _FakeEvent("2026-07-04T10:05:00Z", name="Alarm - 'PUERTA JARDIN'", type="triggered", zone_id=12)
    ]
    monkeypatch.setattr(engine, "fetch_events", _async_return(events))
    asyncio.run(engine._run_event_scan(config))
    assert bypass_calls == []

    # Second trigger -> count reaches max_retries=2 -> zone gets bypassed.
    events = events + [
        _FakeEvent("2026-07-04T10:06:00Z", name="Alarm - 'PUERTA JARDIN'", type="triggered", zone_id=12)
    ]
    monkeypatch.setattr(engine, "fetch_events", _async_return(events))
    asyncio.run(engine._run_event_scan(config))
    assert bypass_calls == [(12, True)]
    assert telemetry_calls[-1][0][:2] == ("security", "auto_bypass")
    assert telemetry_calls[-1][1]["entity_id"] == "12"
    assert telemetry_calls[-1][1]["payload"]["trigger_count"] == 2

    session = session_cfg.load_override_session()
    assert session.auto_bypassed_zones == [12]
    assert session.session_counts.get("12", 0) == 0

    # A third trigger before the next arm must not re-bypass (already bypassed;
    # RISCO itself won't re-trigger an omitted zone, but the count still tracks).
    events = events + [
        _FakeEvent("2026-07-04T10:07:00Z", name="Alarm - 'PUERTA JARDIN'", type="triggered", zone_id=12)
    ]
    monkeypatch.setattr(engine, "fetch_events", _async_return(events))
    asyncio.run(engine._run_event_scan(config))
    assert bypass_calls == [(12, True)]  # unchanged - only one bypass call so far

    # The next arm event restores the zone and resets the session.
    events = events + [
        _FakeEvent("2026-07-04T11:00:00Z", name="Full Set - 'USER 1 MASTER', WEB", type="armed")
    ]
    monkeypatch.setattr(engine, "fetch_events", _async_return(events))
    asyncio.run(engine._run_event_scan(config))
    assert bypass_calls[-1] == (12, False)
    assert telemetry_calls[-1][0][:2] == ("security", "auto_unbypass")

    session = session_cfg.load_override_session()
    assert session.auto_bypassed_zones == []
    assert session.session_counts == {}


def test_scan_skips_fetch_when_nothing_configured_or_pending(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(override_cfg, "OVERRIDES_PATH", tmp_path / "security_override.json")
    monkeypatch.setattr(session_cfg, "SESSION_PATH", tmp_path / "security_override_session.json")

    called = {"n": 0}

    async def fake_fetch_events():
        called["n"] += 1
        return []

    monkeypatch.setattr(engine, "fetch_events", fake_fetch_events)

    config = engine.OverrideAutomationConfig(enabled=True, event_scan_interval_s=20)
    asyncio.run(engine._run_event_scan(config))

    assert called["n"] == 0  # no overrides configured, nothing pending restoration
