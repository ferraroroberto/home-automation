"""Record + (conditionally) notify on UPS mains-power transitions.

Mirrors :mod:`app.webapp.alarm_notify` for the power domain. Two entry points
used by the background power monitor:

- :func:`record_power_event` — the UPS crossed between mains and battery.
- :func:`record_low_battery_shutdown` / :func:`record_low_battery_shutdown_cancelled`
  — the safety-net pair for the low-battery auto-shutdown: fired when the UPS
  runtime drops critically low while on battery, and when mains power returns
  before the scheduled shutdown completes.

All of them share the same three-step shape:

1. **Always** appends the event to the local activity log (``logs/power.jsonl``).
2. **Conditionally** sends a Telegram message — only when the matching
   :class:`PowerNotifyPrefs` toggle is on and a notifier is configured, via
   :func:`_send_with_retry` (one short retry on a transient failure — see its
   docstring). A delivery failure that survives the retry is logged and
   swallowed so it can never break the monitor.
3. For the shutdown pair, the OS-level action (:mod:`src.host_shutdown`) is
   invoked through an injectable seam so tests never touch the real ``shutdown``
   command.

Edge-triggering (fire only on a transition/threshold-crossing, with a baseline
on first observation) is the caller's job — see :mod:`app.webapp.power_monitor`.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Optional, Tuple

from src.activity_log import append_activity
from src.host_shutdown import cancel_shutdown, initiate_shutdown
from src.notify import Notifier, NotifierError
from src.notify_config import build_alarm_notifier
from src.power_notify_prefs import PowerNotifyPrefs, load_power_notify_prefs

logger = logging.getLogger(__name__)

# One short retry before giving up on a Telegram send. These power alerts are
# edge-triggered and fired exactly once per transition (see power_monitor.py)
# — with no retry, a single transient failure (e.g. the router itself
# rebooting for a moment right as mains power returns) permanently loses that
# one alert (issue #394). Kept short since the send is synchronous/blocking.
# Read fresh on every call (not a default-arg binding) so tests can
# monkeypatch it directly.
_SEND_RETRY_DELAYS_S: Tuple[float, ...] = (3.0,)


def _send_with_retry(notifier: Notifier, text: str) -> None:
    """Send ``text`` via ``notifier``, retrying per :data:`_SEND_RETRY_DELAYS_S`.

    Raises the last :class:`NotifierError` only once every retry is
    exhausted — the caller decides whether to log/swallow it, same as a bare
    ``notifier.send_text`` call.
    """

    last_exc: Optional[NotifierError] = None
    for attempt in range(len(_SEND_RETRY_DELAYS_S) + 1):
        try:
            notifier.send_text(text)
            return
        except NotifierError as exc:
            last_exc = exc
            if attempt < len(_SEND_RETRY_DELAYS_S):
                time.sleep(_SEND_RETRY_DELAYS_S[attempt])
    assert last_exc is not None
    raise last_exc


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
        _send_with_retry(notifier, _compose_message(lost, detail))
    except NotifierError as exc:  # delivery must never break the monitor loop
        logger.warning("⚠️ Telegram power notification failed: %s", exc)


def _compose_shutdown_message(detail: Optional[str]) -> str:
    suffix = f" · {detail}" if detail else ""
    return f"🔴 UPS battery critically low — PC shutting down now{suffix}"


def record_low_battery_shutdown(
    *,
    detail: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
    grace_seconds: int = 180,
    prefs_loader: Callable[[], PowerNotifyPrefs] = load_power_notify_prefs,
    notifier_factory: Callable[[], Optional[Notifier]] = build_alarm_notifier,
    shutdown_fn: Callable[..., bool] = initiate_shutdown,
) -> bool:
    """Log a critically-low UPS battery and, if enabled, alert + shut down.

    Gated entirely on ``auto_shutdown_low_battery`` — off means the event is
    still logged (mirrors every other UPS power event) but neither the
    Telegram alert nor the shutdown fires. Returns ``True`` only when a
    shutdown was actually scheduled.
    """

    record: Dict[str, Any] = {"event": "low_battery_shutdown"}
    if detail:
        record["detail"] = detail
    if extra:
        record.update(extra)
    append_activity("power", record)

    prefs = prefs_loader()
    if not prefs.auto_shutdown_low_battery:
        return False

    notifier = notifier_factory()
    if notifier is not None:
        try:
            _send_with_retry(notifier, _compose_shutdown_message(detail))
        except NotifierError as exc:  # delivery must never break the monitor loop
            logger.warning("⚠️ Telegram low-battery shutdown notification failed: %s", exc)

    return shutdown_fn(
        grace_seconds=grace_seconds,
        message="Low UPS battery — PC shutting down to avoid data loss",
    )


def record_low_battery_shutdown_cancelled(
    *,
    cancel_fn: Callable[[], bool] = cancel_shutdown,
) -> None:
    """Log + cancel a previously-scheduled low-battery shutdown.

    Called when mains power returns before the scheduled OS shutdown
    completes. Cancelling is unconditional (not gated on the toggle) — it is
    always safe/idempotent to cancel a shutdown that may or may not actually
    be pending.
    """

    append_activity("power", {"event": "low_battery_shutdown_cancelled"})
    cancel_fn()
