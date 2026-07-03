"""OS-level graceful shutdown for the UPS low-battery safety trigger.

Wraps Windows ``shutdown.exe``. Deliberately thin — :func:`initiate_shutdown`
and :func:`cancel_shutdown` are the only two operations, both easily
monkeypatched in tests. Hard-blocked under pytest (mirrors
:func:`src.notify_config.build_alarm_notifier`'s pytest guard) so the backend
test suite can never trigger a real shutdown on a dev or CI machine.
"""

from __future__ import annotations

import logging
import subprocess
import sys

logger = logging.getLogger("host_shutdown")

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0


def initiate_shutdown(*, grace_seconds: int = 180, message: str = "") -> bool:
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
            timeout=10,
            creationflags=_NO_WINDOW,
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
            timeout=10,
            creationflags=_NO_WINDOW,
        )
    except OSError as exc:
        logger.warning("⚠️ Failed to invoke shutdown /a: %s", exc)
        return False
    if result.returncode == 0:
        logger.info("✅ Pending host shutdown cancelled (mains power restored)")
    return result.returncode == 0
