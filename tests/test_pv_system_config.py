"""Unit tests for the PV-array config loader (issue #555).

Covers the current multi-sub-array shape, the legacy single-orientation flat
shape (which must keep loading unmigrated), and the malformed-entry/absent-file
fallbacks that make the forecast fail quiet rather than 500.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.pv_system_config import load_pv_system_config


def _write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "pv_system.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_missing_file_returns_none(tmp_path: Path) -> None:
    assert load_pv_system_config(tmp_path / "nope.json") is None


def test_malformed_json_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "pv_system.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_pv_system_config(path) is None


def test_multi_array_shape_loads_all_sub_arrays(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {
            "arrays": [
                {"kwp": 7.9, "tilt_deg": 15, "azimuth_deg": 0},
                {"kwp": 0.9, "tilt_deg": 15, "azimuth_deg": 180},
            ],
            "performance_ratio": 0.8,
        },
    )
    config = load_pv_system_config(path)
    assert config is not None
    assert len(config.arrays) == 2
    assert config.arrays[0].kwp == 7.9
    assert config.arrays[1].azimuth_deg == 180
    assert config.performance_ratio == 0.8
    assert config.total_kwp == 8.8


def test_legacy_flat_shape_loads_as_one_implicit_array(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {"kwp": 5.0, "tilt_deg": 30, "azimuth_deg": 0, "performance_ratio": 0.8},
    )
    config = load_pv_system_config(path)
    assert config is not None
    assert len(config.arrays) == 1
    assert config.arrays[0].kwp == 5.0
    assert config.arrays[0].tilt_deg == 30
    assert config.total_kwp == 5.0


def test_a_malformed_sub_array_is_skipped_not_fatal(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {
            "arrays": [
                {"kwp": 7.9, "tilt_deg": 15, "azimuth_deg": 0},
                {"tilt_deg": 15, "azimuth_deg": 180},  # missing kwp — skipped
            ],
        },
    )
    config = load_pv_system_config(path)
    assert config is not None
    assert len(config.arrays) == 1
    assert config.arrays[0].kwp == 7.9


def test_all_sub_arrays_malformed_is_not_configured(tmp_path: Path) -> None:
    path = _write(tmp_path, {"arrays": [{"tilt_deg": 15}, {"kwp": -1}]})
    assert load_pv_system_config(path) is None


def test_empty_arrays_list_is_not_configured(tmp_path: Path) -> None:
    path = _write(tmp_path, {"arrays": []})
    assert load_pv_system_config(path) is None


def test_tilt_and_azimuth_are_clamped_per_array(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {"arrays": [{"kwp": 1.0, "tilt_deg": 999, "azimuth_deg": -999}]},
    )
    config = load_pv_system_config(path)
    assert config is not None
    assert config.arrays[0].tilt_deg == 90.0
    assert config.arrays[0].azimuth_deg == -180.0


def test_performance_ratio_default_and_clamp(tmp_path: Path) -> None:
    path = _write(tmp_path, {"arrays": [{"kwp": 1.0}], "performance_ratio": 5.0})
    config = load_pv_system_config(path)
    assert config is not None
    assert config.performance_ratio == 1.0
