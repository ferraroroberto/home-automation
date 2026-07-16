"""Regression coverage for schedule/presence alarm-command coordination."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import app.webapp.presence_automation as presence_automation
import app.webapp.security_automation as security_automation
from src import presence_engine
from src.risco_client import RiscoCommandError
from src.security_schedules import SecurityScheduleEntry


class _FakeSecurity:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.ongoing_alarm = False
        self.memory_alarm = False
        self.ac_lost = False


_NIGHT_ENTRY = SecurityScheduleEntry(
    id="night",
    enabled=True,
    time="21:00",
    days=["wed"],
    action="perimeter",
)


async def _async_noop(**_kwargs) -> None:
    return None


def _wire_presence_tick(
    monkeypatch,
    *,
    panel: _FakeSecurity,
    read_perimeter: asyncio.Event,
    confirm,
    record,
) -> None:
    async def fake_fetch_security_state() -> _FakeSecurity:
        if panel.mode == "perimeter":
            read_perimeter.set()
        return panel

    config = presence_engine.PresenceAutomationConfig(enabled=True, stale_after_s=3600)
    monkeypatch.setattr(presence_automation, "fetch_security_state", fake_fetch_security_state)
    monkeypatch.setattr(presence_automation, "check_security_transitions", _async_noop)
    monkeypatch.setattr(presence_automation, "consider_security_read", lambda _state: None)
    monkeypatch.setattr(presence_automation, "consider_security_override", lambda _state: None)
    monkeypatch.setattr(presence_automation, "load_automation_config", lambda: config)
    monkeypatch.setattr(presence_automation, "load_hidden_presence_ids", lambda: set())
    monkeypatch.setattr(presence_automation, "load_kids_home_override", lambda: False)
    monkeypatch.setattr(presence_automation, "confirm_alarm_action", confirm)
    monkeypatch.setattr(presence_automation, "record_alarm_action", record)
    monkeypatch.setattr(presence_automation, "send_push", lambda *_args: None)
    monkeypatch.setattr(presence_automation, "append_trigger_log", lambda _event: None)


def test_schedule_arm_suppresses_an_older_unconsumed_arrival(monkeypatch, tmp_path) -> None:
    """A pre-schedule arrival must not disarm an in-flight scheduled arm.

    This reproduces the 2026-07-15 ordering: the schedule first reaches
    perimeter, the presence loop observes that intermediate state while the
    schedule is still confirming, and the schedule later finishes.  Before
    issue #449, presence issued a real disarm in that window.
    """

    monkeypatch.setattr(presence_engine, "STATE_PATH", tmp_path / "presence_state.json")
    arrival_at = datetime.now(timezone.utc) - timedelta(minutes=24)
    presence_engine.set_person_state("member", "home", at=arrival_at)

    panel = _FakeSecurity("disarmed")
    schedule_reached_perimeter = asyncio.Event()
    presence_read_perimeter = asyncio.Event()
    finish_schedule = asyncio.Event()
    presence_commands: list[str] = []
    presence_records: list[dict] = []

    async def fake_schedule_confirm(action: str) -> _FakeSecurity:
        assert action == "perimeter"
        panel.mode = "perimeter"
        schedule_reached_perimeter.set()
        await finish_schedule.wait()
        panel.mode = "perimeter"  # the schedule's retry restores the final state
        return panel

    async def fake_presence_confirm(action: str) -> _FakeSecurity:
        presence_commands.append(action)
        panel.mode = "disarmed"
        return panel

    async def fake_presence_record(**kwargs) -> None:
        presence_records.append(kwargs)

    monkeypatch.setattr(security_automation, "confirm_alarm_action", fake_schedule_confirm)
    monkeypatch.setattr(security_automation, "record_alarm_action", _async_noop)
    _wire_presence_tick(
        monkeypatch,
        panel=panel,
        read_perimeter=presence_read_perimeter,
        confirm=fake_presence_confirm,
        record=fake_presence_record,
    )

    async def run_race() -> None:
        schedule_task = asyncio.create_task(security_automation._apply_schedule(_NIGHT_ENTRY))
        await schedule_reached_perimeter.wait()

        presence_task = asyncio.create_task(presence_automation.tick())
        await presence_read_perimeter.wait()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        finish_schedule.set()
        await asyncio.gather(schedule_task, presence_task)

    asyncio.run(run_race())

    assert panel.mode == "perimeter"
    assert presence_commands == []
    assert presence_records == []


def test_genuine_arrival_waits_for_schedule_then_disarms(monkeypatch, tmp_path) -> None:
    """An arrival after the scheduled arm remains valid, without overlapping it."""

    monkeypatch.setattr(presence_engine, "STATE_PATH", tmp_path / "presence_state.json")
    panel = _FakeSecurity("disarmed")
    schedule_reached_perimeter = asyncio.Event()
    presence_read_perimeter = asyncio.Event()
    finish_schedule = asyncio.Event()
    order: list[str] = []

    async def fake_schedule_confirm(_action: str) -> _FakeSecurity:
        order.append("schedule-start")
        panel.mode = "perimeter"
        schedule_reached_perimeter.set()
        await finish_schedule.wait()
        order.append("schedule-finish")
        return panel

    async def fake_presence_confirm(action: str) -> _FakeSecurity:
        order.append(f"presence-{action}")
        panel.mode = "disarmed"
        return panel

    monkeypatch.setattr(security_automation, "confirm_alarm_action", fake_schedule_confirm)
    monkeypatch.setattr(security_automation, "record_alarm_action", _async_noop)
    _wire_presence_tick(
        monkeypatch,
        panel=panel,
        read_perimeter=presence_read_perimeter,
        confirm=fake_presence_confirm,
        record=_async_noop,
    )

    async def run_race() -> None:
        schedule_task = asyncio.create_task(security_automation._apply_schedule(_NIGHT_ENTRY))
        await schedule_reached_perimeter.wait()
        # This transition is deliberately newer than the schedule marker.
        presence_engine.set_person_state(
            "member",
            "home",
            at=datetime.now(timezone.utc) + timedelta(seconds=1),
        )

        presence_task = asyncio.create_task(presence_automation.tick())
        await presence_read_perimeter.wait()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert order == ["schedule-start"]

        finish_schedule.set()
        await asyncio.gather(schedule_task, presence_task)

    asyncio.run(run_race())

    assert order == ["schedule-start", "schedule-finish", "presence-disarm"]
    assert panel.mode == "disarmed"


def test_failed_schedule_does_not_mask_a_later_arrival(monkeypatch, tmp_path) -> None:
    """A failed arm marker suppresses only transitions older than its request."""

    monkeypatch.setattr(presence_engine, "STATE_PATH", tmp_path / "presence_state.json")

    async def fake_confirm(_action: str) -> _FakeSecurity:
        raise RiscoCommandError("panel down")

    monkeypatch.setattr(security_automation, "confirm_alarm_action", fake_confirm)
    monkeypatch.setattr(security_automation, "record_alarm_action", _async_noop)

    with pytest.raises(RiscoCommandError, match="panel down"):
        asyncio.run(security_automation._apply_schedule(_NIGHT_ENTRY))

    later = datetime.now(timezone.utc) + timedelta(seconds=1)
    person = presence_engine.PersonPresence(
        person_id="member",
        state="home",
        updated_at=later,
        state_since=later,
    )
    decision = presence_engine.evaluate_alarm_decision(
        [person],
        security_mode="perimeter",
        config=presence_engine.PresenceAutomationConfig(enabled=True, stale_after_s=3600),
        at=later + timedelta(seconds=1),
    )

    assert decision is not None
    assert decision.kind == "disarm"
