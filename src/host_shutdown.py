"""OS-level graceful shutdown for the UPS low-battery safety trigger.

Wraps Windows ``shutdown.exe``. Deliberately thin — :func:`initiate_shutdown`,
:func:`initiate_hibernate` and :func:`cancel_shutdown` are the operations, all easily
monkeypatched in tests. Hard-blocked under pytest (mirrors
:func:`src.notify_config.build_alarm_notifier`'s pytest guard) so the backend
test suite can never trigger a real shutdown on a dev or CI machine.
"""

from __future__ import annotations

import logging
import subprocess
import sys

from src._console_encoding import console_encoding
from src._no_window import NO_WINDOW

logger = logging.getLogger("host_shutdown")

# Grace window between the low-UPS-battery trigger and the tower going down:
# time for apps to save and close, and for mains to return and cancel it. Stated
# guess that it fits inside the UPS runtime left at the trigger; the single value
# the alert text, the hibernate timer and the shutdown call all share.
LOW_BATTERY_GRACE_S = 180


def initiate_shutdown(*, grace_seconds: int = LOW_BATTERY_GRACE_S, message: str = "") -> bool:
    """Schedule a graceful Windows shutdown after ``grace_seconds``.

    Windows notifies running applications (``WM_QUERYENDSESSION``) during the
    grace window so well-behaved apps get a chance to autosave/close cleanly,
    then forces close at the deadline. Returns ``True`` if Windows accepted the
    scheduled shutdown. Never raises — a failure here must not break the
    caller's alert/log flow.
    """

    if "pytest" in sys.modules:
        logger.info("🧪 Shutdown suppressed under pytest (grace=%ds)", grace_seconds)
        return False
    try:
        result = subprocess.run(
            [
                "shutdown",
                "/s",
                "/t",
                str(grace_seconds),
                "/c",
                message or "Low UPS battery — shutting down to avoid data loss",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding=console_encoding(),
            errors="replace",
            timeout=10,
            creationflags=NO_WINDOW,
        )
    except OSError as exc:
        logger.warning("⚠️ Failed to invoke shutdown /s: %s", exc)
        return False
    if result.returncode != 0:
        logger.warning(
            "⚠️ shutdown /s returned %d: %s",
            result.returncode,
            (result.stderr or result.stdout or "").strip(),
        )
        return False
    logger.warning("🔻 Host shutdown scheduled in %ds (low UPS battery)", grace_seconds)
    return True


def initiate_hibernate() -> bool:
    """Hibernate this machine now: state goes to disk, the session resumes on wake.

    ``shutdown /h`` is the explicit hibernate call (it never degrades to sleep,
    unlike ``SetSuspendState``) and, as Windows has no scheduled hibernate, it
    takes no ``/t`` — the caller owns the grace window. It exits nonzero when
    hibernation is not enabled. Returns ``True`` if Windows accepted it. Never
    raises, and is hard-blocked under pytest like :func:`initiate_shutdown`.
    """

    if "pytest" in sys.modules:
        logger.info("🧪 Hibernate suppressed under pytest")
        return False
    try:
        result = subprocess.run(
            ["shutdown", "/h"],
            check=False,
            capture_output=True,
            text=True,
            encoding=console_encoding(),
            errors="replace",
            timeout=10,
            creationflags=NO_WINDOW,
        )
    except OSError as exc:
        logger.warning("⚠️ Failed to invoke shutdown /h: %s", exc)
        return False
    if result.returncode != 0:
        logger.warning(
            "⚠️ shutdown /h returned %d: %s",
            result.returncode,
            (result.stderr or result.stdout or "").strip(),
        )
        return False
    logger.warning("🔻 Host hibernating (low UPS battery)")
    return True


def cancel_shutdown() -> bool:
    """Abort a pending scheduled shutdown (e.g. mains power returned in time).

    A nonzero return with no shutdown pending is expected/benign and is not
    logged as a failure.
    """

    if "pytest" in sys.modules:
        return False
    try:
        result = subprocess.run(
            ["shutdown", "/a"],
            check=False,
            capture_output=True,
            text=True,
            encoding=console_encoding(),
            errors="replace",
            timeout=10,
            creationflags=NO_WINDOW,
        )
    except OSError as exc:
        logger.warning("⚠️ Failed to invoke shutdown /a: %s", exc)
        return False
    if result.returncode == 0:
        logger.info("✅ Pending host shutdown cancelled (mains power restored)")
    return result.returncode == 0
