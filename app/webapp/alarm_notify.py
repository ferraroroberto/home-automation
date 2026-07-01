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
de-dupe state is persisted to ``logs/alarm_notify_dedupe.json`` so a tray
restart does not reset the counter mid-day. The command is still logged every
attempt, so the activity log shows the retries.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
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

# Per-day error-notify de-dupe: dedupe_key -> "YYYY-MM-DD".
# Persisted to disk so a tray restart does not re-fire the same-day alert.
_DEDUPE_PATH = Path(__file__).resolve().parent.parent.parent / "logs" / "alarm_notify_dedupe.json"


def _load_dedupe() -> Dict[str, str]:
    try:
        return json.loads(_DEDUPE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_dedupe(state: Dict[str, str]) -> None:
    try:
        _DEDUPE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _DEDUPE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_DEDUPE_PATH)
    except OSError as exc:
        logger.warning("⚠️ Could not persist alarm de-dupe state: %s", exc)


_last_error_notify: Dict[str, str] = _load_dedupe()


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
        _save_dedupe(_last_error_notify)

    notifier = notifier_factory()
    if notifier is None:
        return
    try:
        notifier.send_text(_compose_message(source, action, outcome, error, detail))
    except NotifierError as exc:  # delivery must never break the automation loop
        logger.warning("⚠️ Telegram alarm notification failed: %s", exc)


# --------------------------------------------------------------------------
# Panel events: intrusion (alarm triggered) + AC-power lost/restored. These are
# edge-triggered off the live RISCO SecurityState, polled by the presence loop.
# --------------------------------------------------------------------------

# Last-seen panel flags. Process-lifetime; a condition already active at startup
# sets the baseline (no alert) so we only notify on a genuine transition.
_last_security: Dict[str, Optional[bool]] = {"intrusion": None, "ac_lost": None}

_SECURITY_MESSAGES = {
    ("intrusion", True): "🚨 ALARM TRIGGERED at home",
    ("ac_lost", True): "⚠️ Alarm panel lost mains power (on backup battery)",
    ("ac_lost", False): "✅ Alarm panel mains power restored",
}


def record_security_event(
    *,
    kind: str,
    active: bool,
    detail: Optional[str] = None,
    log_detail: Optional[str] = None,
    prefs_loader: Callable[[], AlarmNotifyPrefs] = load_alarm_notify_prefs,
    notifier_factory: Callable[[], Optional[Notifier]] = build_alarm_notifier,
) -> None:
    """Log a panel event (``intrusion`` / ``ac_lost``) and notify per its toggle.

    ``log_detail`` is persisted to the activity log only, never the Telegram
    copy — for the raw ``ongoing_alarm``/``memory_alarm`` flags (issue #307),
    which are diagnosable-from-logs breadcrumbs, not something the owner needs
    on their phone at 🚨 time. ``detail`` (rare, e.g. a manual test note) goes
    to both, as before.
    """

    record: Dict[str, Any] = {"source": "panel", "event": kind, "active": active}
    if detail:
        record["detail"] = detail
    if log_detail:
        record["diagnostic"] = log_detail
    append_activity("alarm", record)

    prefs = prefs_loader()
    if not getattr(prefs, kind, False):
        return
    message = _SECURITY_MESSAGES.get((kind, active))
    if message is None:  # e.g. intrusion clearing — no all-clear message
        return
    if detail:
        message = f"{message} · {detail}"

    notifier = notifier_factory()
    if notifier is None:
        return
    try:
        notifier.send_text(message)
    except NotifierError as exc:
        logger.warning("⚠️ Telegram security notification failed: %s", exc)


def check_security_transitions(
    *,
    intrusion: Optional[bool],
    ac_lost: bool,
    intrusion_detail: Optional[str] = None,
    state: Optional[Dict[str, Optional[bool]]] = None,
    prefs_loader: Callable[[], AlarmNotifyPrefs] = load_alarm_notify_prefs,
    notifier_factory: Callable[[], Optional[Notifier]] = build_alarm_notifier,
) -> None:
    """Compare current panel flags to the last seen and alert on transitions.

    Intrusion alerts only on its *onset* (no all-clear); AC-power alerts on both
    loss and restore. First observation of each flag just sets the baseline.

    ``intrusion`` is ``None`` when the panel's ``ongoing_alarm``/``memory_alarm``
    WebUI scrape came back unreadable this poll (issue #307) — a transient
    scrape hiccup must not be read as "the alarm cleared", or the *next*
    successful poll re-observing a still-latched, days-old ``memory_alarm``
    manufactures a bogus false→true onset. An unreadable poll is skipped
    entirely: the tracked state is left untouched rather than forced to a
    guessed value.
    """

    tracker = _last_security if state is None else state
    log_details = {"intrusion": intrusion_detail}
    for kind, value in (("intrusion", intrusion), ("ac_lost", ac_lost)):
        if value is None:
            continue  # unreadable this poll — don't disturb the tracked state
        last = tracker.get(kind)
        tracker[kind] = value
        if last is None or last == value:
            continue
        if kind == "intrusion" and value is False:
            continue  # intrusion cleared — not an alert
        record_security_event(
            kind=kind,
            active=value,
            log_detail=log_details.get(kind),
            prefs_loader=prefs_loader,
            notifier_factory=notifier_factory,
        )
