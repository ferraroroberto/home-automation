"""Shared load/save shape for all-bool "notify prefs" dataclasses (issue #327).

:class:`src.alarm_notify_prefs.AlarmNotifyPrefs` and
:class:`src.power_notify_prefs.PowerNotifyPrefs` are near-identical frozen
dataclasses of boolean toggles, persisted atomically with the same
load-with-defaults / save shape — only the field names (and the log label)
differ. Parametrized here on the dataclass type so a third toggle-prefs
module reuses this instead of cloning the ~40 LOC pair again.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, fields
from pathlib import Path
from typing import Type, TypeVar

from src._atomic_json import write_json_atomic

logger = logging.getLogger(__name__)

T = TypeVar("T")


def load_toggle_prefs(cls: Type[T], path: Path) -> T:
    """Return saved bool-toggle prefs of type ``cls``, or its defaults when absent/invalid.

    Every field of ``cls`` is coerced with ``bool(...)``, matching the
    per-field ``bool(raw.get(name, default))`` calls this replaces.
    """

    if not path.exists():
        return cls()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("⚠️ Could not read %s (%s); using defaults", path, exc)
        return cls()
    if not isinstance(raw, dict):
        logger.warning("⚠️ %s is not a JSON object; using defaults", path)
        return cls()
    defaults = cls()
    kwargs = {f.name: bool(raw.get(f.name, getattr(defaults, f.name))) for f in fields(cls)}
    return cls(**kwargs)


def save_toggle_prefs(instance: object, path: Path, *, log_label: str) -> None:
    """Atomically persist a bool-toggle prefs instance to ``path``.

    ``log_label`` reproduces each call site's original, distinct log
    message (e.g. ``"alarm notify prefs"`` -> ``"💾 Saved alarm notify prefs
    to %s"``).
    """

    write_json_atomic(path, asdict(instance))
    logger.info("💾 Saved %s to %s", log_label, path)
