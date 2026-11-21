"""Shared exponential-backoff tracker for a persistently-failing source.

:func:`compute_delay` is the escalate-with-a-cap math and :class:`BackoffTracker`
the per-source state around it (consecutive failures and the next retry time).
``ups_client.py`` and ``tuya_client.py`` use the tracker's defaults;
``tesla_client.py``, ``huawei_client.py`` and ``pv_forecast.py`` (Open-Meteo 429s)
each hold one tracker with their own constants, since a cloud API tolerates a
different retry cadence than a local poll. Each client keeps its own locking and log
messages, which differ per source.
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
