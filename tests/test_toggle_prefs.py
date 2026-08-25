"""Unit tests for :mod:`src._toggle_prefs` — the shared bool-toggle load/save shape.

Round-trips against a ``tmp_path`` file using a throwaway dataclass, since the
real callers (``AlarmNotifyPrefs``, ``PowerNotifyPrefs``) add nothing to this
module's own behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from src._schedule_store import StoreUnreadableError
from src._toggle_prefs import load_toggle_prefs, save_toggle_prefs


@dataclass(frozen=True)
class _Prefs:
    notify_a: bool = True
    notify_b: bool = False


def test_load_missing_file_is_defaults(tmp_path: Path) -> None:
    assert load_toggle_prefs(_Prefs, tmp_path / "absent.json") == _Prefs()


def test_save_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "prefs.json"
    save_toggle_prefs(_Prefs(notify_a=False, notify_b=True), path, log_label="test prefs")
    assert load_toggle_prefs(_Prefs, path) == _Prefs(notify_a=False, notify_b=True)


def test_unreadable_file_raises_instead_of_returning_defaults(tmp_path: Path) -> None:
    """Issue #692: corrupt content must not look like "use the defaults" —
    a caller that flips one toggle and saves would wipe every other one."""
    path = tmp_path / "prefs.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(StoreUnreadableError):
        load_toggle_prefs(_Prefs, path)
