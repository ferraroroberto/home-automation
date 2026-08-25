"""Unit tests for :mod:`src.pc_fleet_prefs` — the UPS-shutdown prefs store (#498)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src._schedule_store import StoreUnreadableError
from src.pc_fleet_prefs import PcFleetPrefs, load_pc_fleet_prefs, save_pc_fleet_prefs


def test_save_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "pc_fleet.json"
    save_pc_fleet_prefs(
        PcFleetPrefs(enabled=False, threshold_minutes=30, excluded=("hub-1",)), path
    )
    prefs = load_pc_fleet_prefs(path)
    assert prefs == PcFleetPrefs(enabled=False, threshold_minutes=30, excluded=("hub-1",))


def test_unreadable_file_raises_instead_of_returning_defaults(tmp_path: Path) -> None:
    """Issue #692: corrupt content must not look like "no prefs saved yet" —
    ``update_pc_fleet_prefs`` would save the defaults back over a real
    ``threshold_minutes``/``excluded``."""
    path = tmp_path / "pc_fleet.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(StoreUnreadableError):
        load_pc_fleet_prefs(path)
