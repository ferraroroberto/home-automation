"""Unit tests for :mod:`src.devices.pc_fleet_prefs` — the UPS-shutdown prefs store (#498)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src._schedule_store import StoreUnreadableError
from src.devices.pc_fleet_prefs import PcFleetPrefs, load_pc_fleet_prefs, save_pc_fleet_prefs


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


def test_tower_action_defaults_to_shutdown_and_round_trips(tmp_path: Path) -> None:
    """#962: absent in an older file -> shutdown; hibernate persists."""
    path = tmp_path / "pc_fleet.json"
    path.write_text('{"enabled": true, "threshold_minutes": 15}', encoding="utf-8")
    assert load_pc_fleet_prefs(path).tower_action == "shutdown"

    save_pc_fleet_prefs(PcFleetPrefs(tower_action="hibernate"), path)
    assert load_pc_fleet_prefs(path).tower_action == "hibernate"


@pytest.mark.parametrize("bad", ["sleep", "", None, 3, ["hibernate"]])
def test_unknown_tower_action_falls_back_to_shutdown(tmp_path: Path, bad) -> None:
    path = tmp_path / "pc_fleet.json"
    path.write_text(json.dumps({"tower_action": bad}), encoding="utf-8")
    assert load_pc_fleet_prefs(path).tower_action == "shutdown"

    # A bad in-memory value is also healed on save, never written through.
    save_pc_fleet_prefs(PcFleetPrefs(tower_action=bad), path)  # type: ignore[arg-type]
    assert json.loads(path.read_text(encoding="utf-8"))["tower_action"] == "shutdown"
