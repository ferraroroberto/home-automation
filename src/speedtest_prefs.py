"""Opt-in switch for the nightly internet speed test (#840).

One boolean, **off by default**: a speed test saturates the link and spends
real bandwidth, which is why the Network tile's test has always been
button-only. Turning this on lets :mod:`app.webapp.speedtest_schedule` run it
once a night at the fixed quiet hour below; the result lands in the same
``internet_samples`` series the tile's sparklines read.

Persisted atomically to gitignored ``config/speedtest_prefs.json`` (committed
``…sample.json``), the same all-bool shape as :mod:`src.power_notify_prefs`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src._toggle_prefs import load_toggle_prefs, save_toggle_prefs

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "speedtest_prefs.json"

#: Local hour the nightly test is due (03:00 — nobody is streaming). Fixed on
#: purpose: the setting is on/off, not a scheduler.
NIGHTLY_HOUR = 3


@dataclass(frozen=True)
class SpeedtestPrefs:
    """Speed-test behaviour. Default: nothing runs unless asked."""

    nightly_enabled: bool = False


def load_speedtest_prefs(path: Optional[Path] = None) -> SpeedtestPrefs:
    """Return saved prefs, or the defaults (nightly off) when absent/invalid."""

    target = Path(path) if path is not None else DEFAULT_PATH
    return load_toggle_prefs(SpeedtestPrefs, target)


def save_speedtest_prefs(prefs: SpeedtestPrefs, path: Optional[Path] = None) -> None:
    """Atomically persist the toggle to disk."""

    target = Path(path) if path is not None else DEFAULT_PATH
    save_toggle_prefs(prefs, target, log_label="speed-test prefs")
