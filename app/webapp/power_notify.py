"""Record + (conditionally) notify on UPS mains-power transitions.

Mirrors :mod:`app.webapp.alarm_notify` for the power domain. A single entry
point — :func:`record_power_event` — used by the background power monitor when
the UPS crosses between mains and battery:

1. **Always** appends the event to the local activity log (``logs/power.jsonl``).
2. **Conditionally** sends a Telegram message — only when the matching
   :class:`PowerNotifyPrefs` toggle is on and a notifier is configured. A
   delivery failure is logged and swallowed so it can never break the monitor.

Edge-triggering (fire only on the mains↔battery transition, with a baseline on
first observation) is the caller's job — see :mod:`app.webapp.power_monitor`.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from src.activity_log import append_activity
from src.notify import Notifier, NotifierError
from src.notify_config import build_alarm_notifier
from src.power_notify_prefs import PowerNotifyPrefs, load_power_notify_prefs

logger = logging.getLogger(__name__)


def _compose_message(lost: bool, detail: Optional[str]) -> str:
    suffix = f" · {detail}" if detail else ""
    if lost:
        return f"⚠️ Mains power LOST — PC & Wi-Fi on UPS battery{suffix}"
    return f"✅ Mains power restored — UPS back on mains{suffix}"


def record_power_event(
    *,
    lost: bool,
    detail: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
    prefs_loader: Callable[[], PowerNotifyPrefs] = load_power_notify_prefs,
    notifier_factory: Callable[[], Optional[Notifier]] = build_alarm_notifier,
) -> None:
    """Log a UPS power transition and notify when its toggle is on.

    Args:
        lost: ``True`` when mains was lost (UPS on battery); ``False`` on restore.
        detail: short human context (e.g. battery runtime) for the message.
        prefs_loader / notifier_factory: injection seams for tests.
    """

    event = "power_lost" if lost else "power_restored"
    record: Dict[str, Any] = {"event": event, "mains_online": not lost}
    if detail:
        record["detail"] = detail
    if extra:
        record.update(extra)
    append_activity("power", record)

    prefs = prefs_loader()
    if not getattr(prefs, event, False):
        return

    notifier = notifier_factory()
    if notifier is None:
        return
    try:
        notifier.send_text(_compose_message(lost, detail))
    except NotifierError as exc:  # delivery must never break the monitor loop
        logger.warning("⚠️ Telegram power notification failed: %s", exc)
