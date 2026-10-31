"""``src._spoken_time`` speak helpers — the one home for spoken clock/day forms."""

from datetime import datetime

import pytest

from src._spoken_time import speak_clock, speak_day


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (0, 0, "12 AM"),
        (7, 0, "7 AM"),
        (12, 0, "12 PM"),
        (12, 5, "12:05 PM"),
        (18, 30, "6:30 PM"),
        (23, 59, "11:59 PM"),
    ],
)
def test_speak_clock(hour: int, minute: int, expected: str) -> None:
    assert speak_clock(hour, minute) == expected


def test_speak_day_has_no_zero_padded_day() -> None:
    assert speak_day(datetime(2026, 7, 2)) == "Thursday July 2"
    assert speak_day(datetime(2026, 7, 12)) == "Sunday July 12"
