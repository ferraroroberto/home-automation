"""Shared exponential-backoff tracker for a persistently-failing source (issue #537, #699).

The same escalate-with-a-cap algorithm was hand-rolled three times: ``_SourceBackoff``
in ``ups_client.py`` and ``_DeviceBackoff`` in ``tuya_client.py`` — sharing identical
constants and the same inline comment byte-for-byte — plus the free functions
``_backoff_for``/``_note_failure``/``_note_success`` in ``huawei_client.py`` (its own
constants, since a FusionSolar cloud login tolerates a different retry cadence than a
local NUT/Tuya poll). This is the single home for the math and the stateful tracker,
following this repo's ``_no_window.py`` / ``_mac.py`` / ``_atomic_json.py`` convention.
Each client keeps its own constants, locking, and log messages — those genuinely
differ per source.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


def compute_delay(
    streak: int,
    *,
    base_s: float = 15.0,
    max_s: float = 300.0,
    factor: float = 2.0,
    max_exponent: int = 10,
) -> float:
    """Delay in seconds after ``streak`` consecutive failures (``streak`` >= 1).

    ``max_exponent`` caps the exponent so a source dead for days can't grow
    ``factor ** exponent`` into an ``OverflowError`` — at the defaults,
    ``base_s * factor**max_exponent`` already far exceeds ``max_s``.
    """
    exponent = min(streak - 1, max_exponent)
    return min(max_s, base_s * (factor ** exponent))


@dataclass
class BackoffTracker:
    """Exponential backoff state for one persistently-failing source.

    Not thread-safe by itself — callers sharing one instance across threads
    (or a ``dict`` of these, keyed per-device/per-source) serialize access
    with their own lock, same as every existing call site already does.
    """

    base_s: float = 15.0
    max_s: float = 300.0
    factor: float = 2.0
    max_exponent: int = 10

    consecutive_failures: int = field(default=0, init=False)
    next_retry_at: float = field(default=0.0, init=False)  # monotonic seconds; 0 == clear

    def seconds_remaining(self) -> Optional[float]:
        """Seconds left in the current backoff window, or ``None`` if clear."""
        remaining = self.next_retry_at - time.monotonic()
        return remaining if remaining > 0 else None

    def record_failure(self) -> float:
        """Escalate after a failed attempt; returns the new delay in seconds."""
        self.consecutive_failures += 1
        delay = compute_delay(
            self.consecutive_failures,
            base_s=self.base_s,
            max_s=self.max_s,
            factor=self.factor,
            max_exponent=self.max_exponent,
        )
        self.next_retry_at = time.monotonic() + delay
        return delay

    def record_success(self) -> None:
        """Clear the backoff after a successful attempt."""
        self.consecutive_failures = 0
        self.next_retry_at = 0.0
