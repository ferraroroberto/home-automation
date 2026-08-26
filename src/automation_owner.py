"""Cross-process, stale-safe lock deciding which webapp process owns the
write-side automation loops (#690).

Two webapp instances can end up pointed at the same ``config/``, ``logs/``
and physical devices at once — a dev process on a spare port next to the
tray-owned one, or two production-shaped boots after a bad restart. Only one
may run presence automation, security schedules, samplers, wake alarms, the
HA trace collector, etc.; every other instance must still serve the API/PWA,
just without any of those loops running.

The guarantee is an OS-level advisory lock on ``config/.automation-owner.lock``
(``msvcrt.locking`` on Windows, ``fcntl.flock`` elsewhere), held open for the
life of the owning process. Both platforms release such a lock when the
holder's file handle table is torn down — on a clean exit *or* an unclean
kill — so there is no PID-liveness bookkeeping to get wrong: empirically
verified by force-killing a holder process and observing the lock become
acquirable immediately (issue #690). This mirrors the same "OS auto-releases
on process death" guarantee ``app/tray/single_instance.py`` documents for its
named mutex, applied here to a webapp-level, cross-port concern instead of a
tray-level, single-process one.

Fails open (``held=True``) on any locking-primitive glitch — a busted lock
must never silently disable automation everywhere.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_LOCK_PATH = (
    Path(__file__).resolve().parent.parent / "config" / ".automation-owner.lock"
)

_IS_WINDOWS = sys.platform == "win32"


class AutomationOwnership:
    """Acquire-on-construct advisory lock over the write-side automation loops.

    ``held`` is True iff this process may start those loops. Keep the
    instance alive for the process lifetime (hold a reference) — dropping it
    without calling :meth:`release` merely delays the OS-level release to
    garbage collection, not to shutdown.
    """

    def __init__(
        self, lock_path: Optional[Path] = None, port: Optional[int] = None
    ) -> None:
        self.lock_path = Path(lock_path) if lock_path is not None else DEFAULT_LOCK_PATH
        self.port = port
        self._fh = None
        self.owner_info = ""
        self.held = self._acquire()

    def _acquire(self) -> bool:
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self.lock_path, "a+b")
        except OSError as exc:
            logger.warning(
                "⚠️  automation-owner lock: could not open %s (%s) — failing open",
                self.lock_path,
                exc,
            )
            return True

        try:
            fh.seek(0)
            if _IS_WINDOWS:
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.owner_info = self._read_owner(fh)
            fh.close()
            return False
        except Exception as exc:  # noqa: BLE001 — a locking glitch must fail open
            logger.warning(
                "⚠️  automation-owner lock glitch (%s) — failing open", exc
            )
            fh.close()
            return True

        self._write_self(fh)
        self._fh = fh
        return True

    @staticmethod
    def _read_owner(fh) -> str:
        # Windows' msvcrt.locking() is mandatory over its locked byte range —
        # a non-owner's read of byte 0 itself raises PermissionError, not just
        # a competing lock/write. The lock therefore only ever covers byte 0
        # (below); the human-readable "pid=... port=..." info always starts
        # at byte 1 so a denied caller can still read it.
        try:
            fh.seek(1)
            return fh.read().decode("utf-8", errors="replace").strip()
        except OSError:
            return ""

    def _write_self(self, fh) -> None:
        fh.seek(1)
        fh.truncate()
        fh.write(f"pid={os.getpid()} port={self.port}\n".encode("utf-8"))
        fh.flush()

    def release(self) -> None:
        """Drop the handle. Idempotent. Called on clean shutdown."""
        if self._fh is None:
            return
        try:
            self._fh.seek(0)
            if _IS_WINDOWS:
                import msvcrt

                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "AutomationOwnership":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


__all__ = ["AutomationOwnership", "DEFAULT_LOCK_PATH"]
