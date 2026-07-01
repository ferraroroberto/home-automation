"""In-memory, app-native countdown timers (issue #304).

Deliberately not persisted — mirrors Home Assistant's own ephemeral,
voice-set timers (no official poll API exists for those; see the issue).
A webapp restart clears any active timer, same as a Voice PE restart would
clear its own. Single-process asyncio access only, no locking needed.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_SECONDS = 24 * 60 * 60  # a day — generous ceiling against a bad client value


@dataclass
class WakeTimerEntry:
    """One active (or just-expired, awaiting dismissal) countdown timer."""

    id: str
    label: str
    seconds: int  # original requested duration
    ends_at: float  # time.time() this timer expires/expired
    ringing: bool = False


_TIMERS: Dict[str, WakeTimerEntry] = {}


def create_timer(label: str, seconds: int) -> WakeTimerEntry:
    """Start a new countdown timer and return it."""

    clean_seconds = max(1, min(int(seconds), MAX_SECONDS))
    timer_id = uuid.uuid4().hex[:12]
    entry = WakeTimerEntry(
        id=timer_id,
        label=str(label or "").strip()[:80],
        seconds=clean_seconds,
        ends_at=time.time() + clean_seconds,
    )
    _TIMERS[timer_id] = entry
    logger.info("⏱️ Timer started: %s (%ss)", entry.label or entry.id, clean_seconds)
    return entry


def list_timers() -> List[WakeTimerEntry]:
    """Return every active/ringing timer, soonest-expiring first."""

    return sorted(_TIMERS.values(), key=lambda t: t.ends_at)


def cancel_timer(timer_id: str) -> bool:
    """Remove a timer (active or ringing). ``True`` if one was removed."""

    return _TIMERS.pop(timer_id, None) is not None


def mark_expired(now: Optional[float] = None) -> List[WakeTimerEntry]:
    """Flip ``ringing`` on any timer whose countdown just elapsed.

    Returns the timers that transitioned this call (for notify purposes) —
    an already-ringing timer isn't returned again.
    """

    instant = now if now is not None else time.time()
    newly_expired: List[WakeTimerEntry] = []
    for entry in _TIMERS.values():
        if not entry.ringing and entry.ends_at <= instant:
            entry.ringing = True
            newly_expired.append(entry)
    return newly_expired
