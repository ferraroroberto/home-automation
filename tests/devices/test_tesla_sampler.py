"""Tesla sampler: sleep-friendly cadence; an asleep car stores nothing."""

from __future__ import annotations

import asyncio
import logging
import time

from app.webapp import tesla_sampler as ts
from src.devices import tesla_history as th
from src.devices.tesla_client import TeslaReading, TeslaSample

CONFIG = ts.TeslaSamplerConfig(enabled=True, active_interval_s=120, idle_interval_s=600,
                               idle_before_quiet_s=900, quiet_s=1200)


def _sample(**kw) -> TeslaSample:
    base = dict(ts=1, battery_pct=60.0, range_km=300.0, charging_state="Disconnected",
                charger_power_kw=0.0, charge_energy_added_kwh=0.0, odometer_km=100.0,
                shift_state=None, speed_kmh=None, at_home=True)
    base.update(kw)
    return TeslaSample(**base)


def test_cadence_fast_while_active_slow_while_idle() -> None:
    state = ts.CadenceState()
    assert ts.read_due(state, CONFIG, 0)
    ts.note_reading(state, CONFIG, TeslaReading("online", _sample(shift_state="D", speed_kmh=50.0)), 0)
    assert not ts.read_due(state, CONFIG, 119) and ts.read_due(state, CONFIG, 120)
    ts.note_reading(state, CONFIG, TeslaReading("online", _sample()), 120)
    assert not ts.read_due(state, CONFIG, 600) and ts.read_due(state, CONFIG, 720)


def test_idle_car_gets_a_quiet_window_to_fall_asleep() -> None:
    state = ts.CadenceState()
    ts.note_reading(state, CONFIG, TeslaReading("online", _sample()), 0)
    ts.note_reading(state, CONFIG, TeslaReading("online", _sample()), 600)
    assert ts.data_allowed(state, 600)
    ts.note_reading(state, CONFIG, TeslaReading("online", _sample()), 1200)  # idle 1200 s >= 900
    assert not ts.data_allowed(state, 1800)
    # A state-only check in the window must not restart or re-trigger the idle clock.
    ts.note_reading(state, CONFIG, TeslaReading("online"), 1800)
    assert state.idle_since is None
    assert ts.data_allowed(state, 2400)


def test_asleep_tick_stores_nothing_and_logs_asleep(tmp_path, monkeypatch, caplog) -> None:
    db = tmp_path / "tesla.sqlite3"
    monkeypatch.setattr(th, "DEFAULT_DB_PATH", db)
    th.init_db()
    asked = []

    async def fake_read(*, with_data: bool = True, known_awake: bool = False) -> TeslaReading:
        asked.append(with_data)
        return TeslaReading("asleep")

    monkeypatch.setattr(ts.tesla_client, "read_vehicle", fake_read)
    with caplog.at_level(logging.INFO, logger=ts.logger.name):
        asyncio.run(ts._tick(CONFIG, th.TeslaHistoryConfig(), ts.CadenceState()))

    assert asked == [True]
    latest = th.latest()
    assert latest["sample"] is None and latest["state"] == "asleep"
    assert any("asleep" in r.getMessage() for r in caplog.records)


def test_online_tick_stores_a_sample(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(th, "DEFAULT_DB_PATH", tmp_path / "tesla.sqlite3")
    th.init_db()

    async def fake_read(*, with_data: bool = True, known_awake: bool = False) -> TeslaReading:
        return TeslaReading("online", _sample(ts=int(time.time())))

    monkeypatch.setattr(ts.tesla_client, "read_vehicle", fake_read)
    asyncio.run(ts._tick(CONFIG, th.TeslaHistoryConfig(), ts.CadenceState()))

    sample = th.latest()["sample"]
    assert sample["battery_pct"] == 60.0 and sample["range_km"] == 300.0
    assert sample["charging_state"] == "Disconnected" and sample["odometer_km"] == 100.0


def test_disabled_by_default(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("TESLA_SAMPLER_ENABLED", raising=False)
    monkeypatch.setattr(ts, "load_dotenv", lambda **kw: None)
    monkeypatch.setattr(th, "DEFAULT_DB_PATH", tmp_path / "tesla.sqlite3")
    assert ts.start_tesla_sampler() is None
