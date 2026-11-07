"""The household's civil time zone, shared by every wall-clock rule.

Schedules and panel stamps are written in Europe/Madrid wall time; an IANA
zone (never a fixed offset) keeps both correct across the March and October
daylight-saving changes without any manual adjustment.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

HOME_TZ = ZoneInfo("Europe/Madrid")


def to_home_wall(instant: datetime) -> datetime:
    """Return ``instant`` as an aware Europe/Madrid datetime.

    A naive value is taken to already be Madrid wall time (the host clock and
    the fixed test clocks both are), so it is tagged rather than shifted.
    """

    if instant.tzinfo is None:
        return instant.replace(tzinfo=HOME_TZ)
    return instant.astimezone(HOME_TZ)
