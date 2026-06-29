"""Per-event toggles for automatic-alarm Telegram notifications.

Five booleans controlling which *automatic* alarm events push a Telegram
message (manual arm/disarm from the app never notifies, so it has no toggle):

- ``schedule_arm``    — the weekly schedule armed the alarm
- ``schedule_disarm`` — the weekly schedule disarmed the alarm
- ``presence_arm``    — presence automation armed (everyone away)
- ``presence_disarm`` — presence automation disarmed (someone arrived)
- ``error``           — any automatic arm/disarm *attempt failed*

Default: **only ``error`` is on** — the failure case is the one the user can't
otherwise see; the success notifications are opt-in. Persisted atomically to the
gitignored ``config/alarm_notify_prefs.json`` (committed
``…sample.json``), reusing the load/save shape from ``display_names.py``.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("alarm_notify_prefs")

DEFAULT_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "alarm_notify_prefs.json"
)


@dataclass(frozen=True)
class AlarmNotifyPrefs:
    """Which automatic alarm events notify. Defaults: only failures."""

    schedule_arm: bool = False
    schedule_disarm: bool = False
    presence_arm: bool = False
    presence_disarm: bool = False
    error: bool = True


def load_alarm_notify_prefs(path: Optional[Path] = None) -> AlarmNotifyPrefs:
    """Return saved prefs, or the defaults (only ``error``) when absent/invalid."""

    target = Path(path) if path is not None else DEFAULT_PATH
    if not target.exists():
        return AlarmNotifyPrefs()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("⚠️ Could not read %s (%s); using defaults", target, exc)
        return AlarmNotifyPrefs()
    if not isinstance(raw, dict):
        logger.warning("⚠️ %s is not a JSON object; using defaults", target)
        return AlarmNotifyPrefs()
    defaults = AlarmNotifyPrefs()
    return AlarmNotifyPrefs(
        schedule_arm=bool(raw.get("schedule_arm", defaults.schedule_arm)),
        schedule_disarm=bool(raw.get("schedule_disarm", defaults.schedule_disarm)),
        presence_arm=bool(raw.get("presence_arm", defaults.presence_arm)),
        presence_disarm=bool(raw.get("presence_disarm", defaults.presence_disarm)),
        error=bool(raw.get("error", defaults.error)),
    )


def save_alarm_notify_prefs(
    prefs: AlarmNotifyPrefs, path: Optional[Path] = None
) -> None:
    """Atomically persist the toggles to disk."""

    target = Path(path) if path is not None else DEFAULT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(prefs), indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)
    logger.info("💾 Saved alarm notify prefs to %s", target)
