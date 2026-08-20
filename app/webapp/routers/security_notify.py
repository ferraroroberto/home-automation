"""Automatic-alarm notification toggles CRUD over ``src.alarm_notify_prefs``.

Split out of ``routers/security.py`` (issue #346) — same rationale as
``security_schedules.py``: the seven per-event Telegram toggles are fully
self-contained and share no state with the live RISCO read/write path that
stays in ``security.py``. The GET/PUT pair itself is the shared
``make_bool_prefs_router`` factory (issue #664), which it splits with
``ups.py``'s power notify-prefs route.
"""

from __future__ import annotations

from app.webapp.routers._helpers import make_bool_prefs_router
from src.alarm_notify_prefs import (
    AlarmNotifyPrefs,
    load_alarm_notify_prefs,
    save_alarm_notify_prefs,
)

router = make_bool_prefs_router(
    load_alarm_notify_prefs,
    save_alarm_notify_prefs,
    AlarmNotifyPrefs,
    path="/api/security/notify-prefs",
    slug="notify_prefs",
    get_doc="Return the automatic-alarm notification toggles + whether Telegram is set up.",
)
