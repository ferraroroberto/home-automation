"""Per-event toggles for UPS mains-power Telegram notifications.

Three booleans controlling UPS power-event behaviour:

- ``power_lost``              — mains lost, the UPS went on-battery (the alert that matters)
- ``power_restored``          — mains came back, the UPS is back online (the all-clear)
- ``auto_shutdown_low_battery`` — safety net: when the UPS is on battery and its
  reported runtime drops to 15 minutes or less, send a Telegram alert **and**
  initiate a graceful Windows shutdown (see :mod:`src.host_shutdown`). Off means
  the feature is fully disabled for this event — no alert, no shutdown.

Default: **all on** — power events are rare and high-value, and the auto-shutdown
is a safety measure against silent data loss. Persisted atomically to gitignored
``config/power_notify_prefs.json`` (committed ``…sample.json``), mirroring
:mod:`src.alarm_notify_prefs`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src._toggle_prefs import load_toggle_prefs, save_toggle_prefs

DEFAULT_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "power_notify_prefs.json"
)


@dataclass(frozen=True)
class PowerNotifyPrefs:
    """Which UPS power events notify. Defaults: all on."""

    power_lost: bool = True
    power_restored: bool = True
    auto_shutdown_low_battery: bool = True


def load_power_notify_prefs(path: Optional[Path] = None) -> PowerNotifyPrefs:
    """Return saved prefs, or the defaults (both on) when absent/invalid."""

    target = Path(path) if path is not None else DEFAULT_PATH
    return load_toggle_prefs(PowerNotifyPrefs, target)


def save_power_notify_prefs(
    prefs: PowerNotifyPrefs, path: Optional[Path] = None
) -> None:
    """Atomically persist the toggles to disk."""

    target = Path(path) if path is not None else DEFAULT_PATH
    save_toggle_prefs(prefs, target, log_label="power notify prefs")
