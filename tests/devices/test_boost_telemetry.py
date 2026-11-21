"""The solar-boost flag survives a telemetry failure.

``_set_boost`` flips ``boost_active`` and the settle timer before recording the
event; a raising telemetry write used to abort the coordinator before the
setpoint write, leaving the unit marked boosted with nothing written.
"""

from __future__ import annotations

import pytest

import app.webapp.automation as auto


@pytest.fixture
def failing_telemetry(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(auto.telemetry, "record_event", boom)


def test_boost_start_does_not_raise_when_telemetry_fails(failing_telemetry) -> None:
    state = auto._EngineState()

    auto._set_boost(state, "u1", "Living", True, 100.0, surplus_w=2400.0, reason="surplus")

    assert state.boost_active["u1"] is True
    assert state.boost_order == ["u1"]


def test_boost_stop_does_not_raise_when_telemetry_fails(failing_telemetry) -> None:
    state = auto._EngineState()
    auto._set_boost(state, "u1", "Living", True, 100.0)

    auto._set_boost(state, "u1", "Living", False, 200.0, reason="surplus gone")

    assert state.boost_active["u1"] is False
    assert state.boost_order == []


def test_boost_hold_does_not_raise_when_telemetry_fails(failing_telemetry) -> None:
    class _Decision:
        reason = "held_margin"
        held = ["u1"]

    state = auto._EngineState()

    auto._log_boost_hold(state, _Decision(), 900.0, {})

    assert state.boost_hold_logged == "u1"
