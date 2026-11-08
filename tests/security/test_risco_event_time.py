"""RISCO event stamps across both Europe/Madrid DST transitions.

The panel's ``...Z`` stamp is its local wall time minus a fixed hour in every
season, so a 05:00 local event reads ``04:00Z`` in summer and in winter alike.
These fixtures mirror that observed shape on both sides of the last-Sunday-of-
March and last-Sunday-of-October changes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from src.security.risco_client import _normalize_event_time

MADRID = ZoneInfo("Europe/Madrid")


def _madrid_wall(utc_stamp: str) -> str:
    instant = datetime.strptime(utc_stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return instant.astimezone(MADRID).strftime("%Y-%m-%d %H:%M")


@pytest.mark.parametrize(
    ("raw", "expected_utc", "expected_local"),
    [
        # October 2026 change (Sun 25 Oct): summer side, then winter side.
        ("2026-10-24T04:00:01Z", "2026-10-24T03:00:01Z", "2026-10-24 05:00"),
        ("2026-10-24T22:00:01Z", "2026-10-24T21:00:01Z", "2026-10-24 23:00"),
        ("2026-10-26T04:00:01Z", "2026-10-26T04:00:01Z", "2026-10-26 05:00"),
        ("2026-11-06T22:00:01Z", "2026-11-06T22:00:01Z", "2026-11-06 23:00"),
        # March 2027 change (Sun 28 Mar): winter side, then summer side.
        ("2027-03-27T04:00:01Z", "2027-03-27T04:00:01Z", "2027-03-27 05:00"),
        ("2027-03-27T22:00:01Z", "2027-03-27T22:00:01Z", "2027-03-27 23:00"),
        ("2027-03-29T04:00:01Z", "2027-03-29T03:00:01Z", "2027-03-29 05:00"),
        ("2027-03-29T20:00:01Z", "2027-03-29T19:00:01Z", "2027-03-29 21:00"),
    ],
)
def test_event_stamp_renders_at_local_wall_time_in_both_seasons(
    raw: str, expected_utc: str, expected_local: str
) -> None:
    normalized = _normalize_event_time(raw)
    assert normalized == expected_utc
    assert _madrid_wall(normalized) == expected_local


def test_event_stamp_unrecognised_format_is_left_untouched() -> None:
    assert _normalize_event_time("not a time") == "not a time"
    assert _normalize_event_time("") == ""


def test_legacy_winter_cursor_still_covers_the_event_it_last_saw(tmp_path) -> None:
    """A cursor saved by the old flat-hour reading must not replay events.

    The old reading stored today's 05:00 CET disarm (raw ``04:00:01Z``) as
    ``03:00:01Z``; the current reading of the same event is ``04:00:01Z``.
    """

    import json

    from src.security.alarm_scene_cursor import (
        load_last_alarm_event_time,
        save_last_alarm_event_time,
    )
    from src.security.security_override_session import (
        load_override_session,
        save_override_session,
    )

    event_now = _normalize_event_time("2026-11-07T04:00:01Z")

    scene = tmp_path / "alarm_scene_cursor.json"
    scene.write_text(json.dumps({"last_alarm_event_time": "2026-11-07T03:00:01Z"}), encoding="utf-8")
    assert load_last_alarm_event_time(scene) == event_now
    save_last_alarm_event_time(event_now, scene)
    assert load_last_alarm_event_time(scene) == event_now  # migrated once, never shifted twice

    session_path = tmp_path / "security_override_session.json"
    session_path.write_text(json.dumps({"last_event_time": "2026-11-07T03:00:01Z"}), encoding="utf-8")
    session = load_override_session(session_path)
    assert session.last_event_time == event_now
    save_override_session(session, session_path)
    assert load_override_session(session_path).last_event_time == event_now


def test_legacy_summer_cursor_is_unchanged() -> None:
    """Summer stamps were already read correctly, so their cursors stay put."""

    from src.security.risco_client import migrate_event_cursor

    assert migrate_event_cursor("2026-10-24T03:00:01Z", None) == "2026-10-24T03:00:01Z"
    assert migrate_event_cursor("2026-11-07T04:00:01Z", 2) == "2026-11-07T04:00:01Z"
