"""Per-event toggles for UPS mains-power Telegram notifications.

Two booleans controlling which UPS power transitions notify:

- ``power_lost``     — mains lost, the UPS went on-battery (the alert that matters)
- ``power_restored`` — mains came back, the UPS is back online (the all-clear)

The former ``auto_shutdown_low_battery`` toggle lived here too, but the
low-battery safety shutdown is now the fleet-wide concern of
:mod:`src.pc_fleet_prefs` (its ``enabled`` master supersedes it — and seeds
once from the old value on migration). This store is purely about the two
mains-transition alerts.

Default: **both on** — power events are rare and high-value. Persisted
atomically to gitignored ``config/power_notify_prefs.json`` (committed
``…sample.json``), mirroring :mod:`src.alarm_notify_prefs`.
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
    """Which UPS power events notify. Defaults: both on."""

    power_lost: bool = True
    power_restored: bool = True


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
