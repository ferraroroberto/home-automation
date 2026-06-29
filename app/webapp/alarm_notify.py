"""Record + (conditionally) notify on every alarm command the app issues.

A single entry point — :func:`record_alarm_action` — used by all three places
that change the RISCO alarm: the weekly schedule engine, the presence engine,
and the manual button route. It does two things:

1. **Always** appends the command + its result to the local activity log
   (``logs/alarm.jsonl`` via :mod:`src.activity_log`) — the local alternative to
   the RISCO cloud event log. Manual actions are logged too.
2. **Conditionally** sends a Telegram message — only for *automatic* sources
   (schedule / presence), only when the matching toggle in
   :class:`AlarmNotifyPrefs` is on, and only when a notifier is configured. A
   delivery failure is logged and swallowed so it can never break an automation
   loop. Manual actions never notify (the user is already at the app).

Persistent failures (an offline panel retried every poll) would otherwise spam:
errors carrying a ``dedupe_key`` notify **once per local day** per key. The
command is still logged every attempt, so the activity log shows the retries.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from src.activity_log import append_activity
from src.alarm_notify_prefs import AlarmNotifyPrefs, load_alarm_notify_prefs
from src.notify import Notifier, NotifierError
from src.notify_config import build_alarm_notifier

logger = logging.getLogger(__name__)

SOURCE_MANUAL = "manual"
SOURCE_SCHEDULE = "schedule"
SOURCE_PRESENCE = "presence"

OUTCOME_OK = "ok"
OUTCOME_ERROR = "error"

# Per-day error-notify de-dupe: dedupe_key -> "YYYY-MM-DD". Process-lifetime
# memory; a panel that stays offline alerts once a day, not every poll.
_last_error_notify: Dict[str, str] = {}


def _verb(action: str) -> str:
    """Map a RISCO action to the arm/disarm axis used by the toggles + copy."""

    return "disarm" if action == "disarm" else "arm"


def _compose_message(
    source: str, action: str, outcome: str, error: Optional[str], detail: Optional[str]
) -> str:
    suffix = f" · {detail}" if detail else ""
    if outcome == OUTCOME_ERROR:
        return (
            f"⚠️ Automatic alarm {_verb(action)} FAILED — {source}{suffix}: "
            f"{error or 'unknown error'}"
        )
    if _verb(action) == "arm":
        return f"🔒 Alarm armed automatically — {source}{suffix}"
    return f"🔓 Alarm disarmed automatically — {source}{suffix}"


def _should_notify(prefs: AlarmNotifyPrefs, source: str, action: str, outcome: str) -> bool:
    if outcome == OUTCOME_ERROR:
        return prefs.error
    return bool(getattr(prefs, f"{source}_{_verb(action)}", False))


def record_alarm_action(
    *,
    source: str,
    action: str,
    outcome: str,
    error: Optional[str] = None,
    detail: Optional[str] = None,
    reason: Optional[str] = None,
    dedupe_key: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
    prefs_loader: Callable[[], AlarmNotifyPrefs] = load_alarm_notify_prefs,
    notifier_factory: Callable[[], Optional[Notifier]] = build_alarm_notifier,
) -> None:
    """Log an alarm command and notify when policy allows.

    Args:
        source: one of ``manual`` / ``schedule`` / ``presence``.
        action: the RISCO action (``arm`` / ``disarm`` / ``partial`` / ``perimeter``).
        outcome: ``ok`` or ``error``.
        error: the failure text (carried verbatim into the message) when ``error``.
        detail: short human context (schedule time, presence reason) for the message.
        reason: stored in the activity log (not the message) for audit.
        dedupe_key: when set, an ``error`` notifies at most once per local day per key.
        now / prefs_loader / notifier_factory: injection seams for tests.
    """

    event_kind = "unset" if _verb(action) == "disarm" else "set"
    record: Dict[str, Any] = {
        "source": source,
        "action": action,
        "event": event_kind,
        "outcome": outcome,
    }
    if error:
        record["error"] = error
    if reason:
        record["reason"] = reason
    if detail:
        record["detail"] = detail
    if extra:
        record.update(extra)
    append_activity("alarm", record)

    # Manual actions are logged but never push — the user is at the app.
    if source == SOURCE_MANUAL:
        return

    prefs = prefs_loader()
    if not _should_notify(prefs, source, action, outcome):
        return

    if outcome == OUTCOME_ERROR and dedupe_key is not None:
        today = (now or datetime.now()).strftime("%Y-%m-%d")
        if _last_error_notify.get(dedupe_key) == today:
            return
        _last_error_notify[dedupe_key] = today

    notifier = notifier_factory()
    if notifier is None:
        return
    try:
        notifier.send_text(_compose_message(source, action, outcome, error, detail))
    except NotifierError as exc:  # delivery must never break the automation loop
        logger.warning("⚠️ Telegram alarm notification failed: %s", exc)
