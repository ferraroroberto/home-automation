"""The alarm-scene cursor: absent means first run, unreadable means unknown.

``EventLogScanner`` re-baselines to the newest event when the cursor is
``None``, silently skipping every unprocessed alarm. A transient read failure
must therefore never look like "no cursor".
"""

from __future__ import annotations

import asyncio
import functools
import logging

import pytest

import app.webapp.alarm_scene_automation as auto
from src import _schedule_store
from src._schedule_store import StoreUnreadableError
from src.security import alarm_scene_cursor as cursor_mod


@pytest.fixture(autouse=True)
def _no_read_retry_delay(monkeypatch) -> None:
    monkeypatch.setattr(_schedule_store, "_READ_RETRY_DELAY_S", 0)


def test_absent_cursor_is_none(tmp_path) -> None:
    assert cursor_mod.load_last_alarm_event_time(tmp_path / "cursor.json") is None


def test_saved_cursor_round_trips(tmp_path) -> None:
    path = tmp_path / "cursor.json"
    cursor_mod.save_last_alarm_event_time("2026-07-03T14:15:00Z", path)
    assert cursor_mod.load_last_alarm_event_time(path) == "2026-07-03T14:15:00Z"


def test_unreadable_cursor_raises_instead_of_reading_as_first_run(tmp_path) -> None:
    path = tmp_path / "cursor.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(StoreUnreadableError):
        cursor_mod.load_last_alarm_event_time(path)


def test_scene_scan_with_an_unreadable_cursor_fails_the_tick_without_re_baselining(
    monkeypatch, tmp_path, caplog
) -> None:
    path = tmp_path / "cursor.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(
        auto, "load_last_alarm_event_time",
        functools.partial(cursor_mod.load_last_alarm_event_time, path=path),
    )
    monkeypatch.setattr(
        auto, "save_last_alarm_event_time",
        functools.partial(cursor_mod.save_last_alarm_event_time, path=path),
    )

    class _Event:
        time = "2026-07-03T14:16:00Z"
        type = auto.ALARM_EVENT_TYPE
        zone_id = 12

    async def fake_fetch_events(*a, **k):
        return [_Event()]

    monkeypatch.setattr(auto, "fetch_events", fake_fetch_events)

    async def fake_run_onset(zones, config):
        raise AssertionError("must not deliver without a readable cursor")

    monkeypatch.setattr(auto, "_run_onset", fake_run_onset)

    with caplog.at_level(logging.WARNING, logger=auto.logger.name):
        asyncio.run(auto._run_event_scan(object(), auto.AlarmSceneConfig()))  # must not raise

    assert "Alarm scene event scan failed" in caplog.text
    assert path.read_text(encoding="utf-8") == "{not json"  # no re-baseline overwrote it
