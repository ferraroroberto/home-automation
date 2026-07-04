"""Per-event toggles for automatic-alarm Telegram notifications.

Five booleans controlling which *automatic* alarm events push a Telegram
message (manual arm/disarm from the app never notifies, so it has no toggle):

- ``schedule_arm``    — the weekly schedule armed the alarm
- ``schedule_disarm`` — the weekly schedule disarmed the alarm
- ``presence_arm``    — presence automation armed (everyone away)
- ``presence_disarm`` — presence automation disarmed (someone arrived)
- ``error``           — any automatic arm/disarm *attempt failed*
- ``intrusion``       — the alarm was *triggered* (ongoing / memory alarm)
- ``ac_lost``         — the alarm panel lost / regained mains power

Defaults: the arm/disarm *successes* are off (opt-in), while ``error`` and the
two adverse panel events (``intrusion``, ``ac_lost``) are **on** — those are the
cases the user can't otherwise see. Persisted atomically to the gitignored
``config/alarm_notify_prefs.json`` (committed ``…sample.json``), reusing the
load/save shape from ``display_names.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src._toggle_prefs import load_toggle_prefs, save_toggle_prefs

DEFAULT_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "alarm_notify_prefs.json"
)


@dataclass(frozen=True)
class AlarmNotifyPrefs:
    """Which alarm events notify. Defaults: arm/disarm successes off; adverse on."""

    schedule_arm: bool = False
    schedule_disarm: bool = False
    presence_arm: bool = False
    presence_disarm: bool = False
    error: bool = True
    intrusion: bool = True
    ac_lost: bool = True


def load_alarm_notify_prefs(path: Optional[Path] = None) -> AlarmNotifyPrefs:
    """Return saved prefs, or the defaults (adverse events on) when absent/invalid."""

    target = Path(path) if path is not None else DEFAULT_PATH
    return load_toggle_prefs(AlarmNotifyPrefs, target)


def save_alarm_notify_prefs(
    prefs: AlarmNotifyPrefs, path: Optional[Path] = None
) -> None:
    """Atomically persist the toggles to disk."""

    target = Path(path) if path is not None else DEFAULT_PATH
    save_toggle_prefs(prefs, target, log_label="alarm notify prefs")
