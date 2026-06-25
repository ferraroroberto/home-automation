"""Unit tests for :mod:`src.camera_presets` — the local PTZ-preset fallback.

Round-trips against a ``tmp_path`` file: add (with token allocation), look up,
remove, and the missing/garbage-file guards. No network, no ONVIF.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import camera_presets as P


def test_list_missing_file_is_empty(tmp_path: Path) -> None:
    assert P.list_local_presets("garden", tmp_path / "absent.json") == []


def test_add_allocates_sequential_tokens(tmp_path: Path) -> None:
    path = tmp_path / "presets.json"
    first = P.add_local_preset("garden", "Position 1", 0.1, 0.2, 0.0, path=path)
    second = P.add_local_preset("garden", "Driveway", -0.4, 0.1, 0.3, path=path)
    assert first["token"] == "1" and second["token"] == "2"
    presets = P.list_local_presets("garden", path)
    assert [p["name"] for p in presets] == ["Position 1", "Driveway"]


def test_add_reuses_lowest_free_token_after_removal(tmp_path: Path) -> None:
    path = tmp_path / "presets.json"
    P.add_local_preset("garden", "Position 1", 0.0, 0.0, None, path=path)
    P.add_local_preset("garden", "Position 2", 0.0, 0.0, None, path=path)
    P.remove_local_preset("garden", "1", path=path)
    # Token 1 is free again → the next save reclaims it, not 3.
    reclaimed = P.add_local_preset("garden", "New", 0.5, 0.5, None, path=path)
    assert reclaimed["token"] == "1"


def test_get_and_remove_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "presets.json"
    P.add_local_preset("garden", "Position 1", 0.1, 0.2, 0.3, path=path)
    got = P.get_local_preset("garden", "1", path=path)
    assert got is not None and got["pan"] == 0.1 and got["zoom"] == 0.3
    P.remove_local_preset("garden", "1", path=path)
    assert P.get_local_preset("garden", "1", path=path) is None
    assert not (tmp_path / "presets.json.tmp").exists()


def test_add_without_position_raises(tmp_path: Path) -> None:
    path = tmp_path / "presets.json"
    with pytest.raises(ValueError):
        P.add_local_preset("garden", "Bad", None, 0.2, 0.0, path=path)


def test_load_non_object_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "presets.json"
    path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    assert P.list_local_presets("garden", path) == []
