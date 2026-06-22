"""Unit tests for :mod:`src.display_names` — atomic display-name persistence.

Round-trips against a ``tmp_path`` file: set, load, clear, and the
atomic-overwrite path. No network, no shared state.
"""

from __future__ import annotations

import json
from pathlib import Path

from src import display_names as D


def test_load_missing_file_is_empty(tmp_path: Path) -> None:
    assert D.load_display_names(tmp_path / "absent.json") == {}


def test_set_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "names.json"
    D.set_display_name("unit-1", "Office", path=path)
    assert D.load_display_names(path) == {"unit-1": "Office"}


def test_set_clear_removes_entry(tmp_path: Path) -> None:
    path = tmp_path / "names.json"
    D.set_display_name("unit-1", "Office", path=path)
    D.set_display_name("unit-2", "Studio", path=path)
    # An empty display name clears that unit's override only.
    D.set_display_name("unit-1", "", path=path)
    assert D.load_display_names(path) == {"unit-2": "Studio"}


def test_set_overwrites_atomically_no_tmp_left_behind(tmp_path: Path) -> None:
    path = tmp_path / "names.json"
    D.set_display_name("unit-1", "Office", path=path)
    D.set_display_name("unit-1", "Renamed", path=path)
    assert D.load_display_names(path) == {"unit-1": "Renamed"}
    # The atomic write uses a .tmp sidecar that os.replace renames away.
    assert not (tmp_path / "names.json.tmp").exists()


def test_load_drops_falsy_values(tmp_path: Path) -> None:
    path = tmp_path / "names.json"
    path.write_text(json.dumps({"unit-1": "Office", "unit-2": ""}), encoding="utf-8")
    # Empty-string overrides are not real overrides — filtered on load.
    assert D.load_display_names(path) == {"unit-1": "Office"}


def test_load_non_object_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "names.json"
    path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    assert D.load_display_names(path) == {}
