"""Unit tests for the PV-array config store (issues #555, #561).

Covers the current multi-sub-array shape, the legacy single-orientation flat
shape (which must keep loading unmigrated), and the malformed-entry/absent-file
fallbacks that make the forecast fail quiet rather than 500.

The save path (#561, the Energy-tab editor) is tested separately below because
it is deliberately the *opposite* contract: strict, raising rather than
clamping, and preserving hand-written keys the app doesn't own.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.pv_system_config import (
    PvArray,
    PvSystemConfig,
    load_pv_system_config,
    save_pv_system_config,
    validate_pv_system,
)


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


# --------------------------------------------------------------- save (#561)


def _config(*arrays: PvArray, ratio: float = 0.8) -> PvSystemConfig:
    return PvSystemConfig(arrays=list(arrays), performance_ratio=ratio)


def test_save_round_trips_through_the_loader(tmp_path: Path) -> None:
    path = tmp_path / "pv_system.json"
    save_pv_system_config(
        _config(
            PvArray(kwp=7.9, tilt_deg=15, azimuth_deg=0),
            PvArray(kwp=0.9, tilt_deg=15, azimuth_deg=180),
        ),
        path,
    )
    config = load_pv_system_config(path)
    assert config is not None
    assert [a.kwp for a in config.arrays] == [7.9, 0.9]
    assert config.arrays[1].azimuth_deg == 180
    assert config.performance_ratio == 0.8


def test_save_preserves_unowned_top_level_keys(tmp_path: Path) -> None:
    """A hand-written ``_doc`` note must survive an edit made in the app."""
    path = _write(tmp_path, {"_doc": "why this array is 8.8 kWp", "kwp": 8.8})
    save_pv_system_config(_config(PvArray(kwp=5.0, tilt_deg=20, azimuth_deg=-30)), path)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["_doc"] == "why this array is 8.8 kWp"
    assert raw["arrays"] == [{"kwp": 5.0, "tilt_deg": 20.0, "azimuth_deg": -30.0}]


def test_save_migrates_a_legacy_flat_file_without_leaving_stale_keys(
    tmp_path: Path,
) -> None:
    """Editing a pre-#555 file writes the ``arrays`` shape and drops the flat
    keys — leaving ``kwp: 8.8`` beside a new list would read as a second,
    contradicting source of truth."""
    path = _write(
        tmp_path,
        {"_doc": "note", "kwp": 8.8, "tilt_deg": 35, "azimuth_deg": 0,
         "performance_ratio": 0.8},
    )
    save_pv_system_config(
        _config(PvArray(kwp=8.8, tilt_deg=35, azimuth_deg=0), ratio=0.75), path
    )

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert set(raw) == {"_doc", "arrays", "performance_ratio"}
    assert raw["performance_ratio"] == 0.75


def test_save_is_atomic_leaving_no_tmp_file(tmp_path: Path) -> None:
    path = tmp_path / "pv_system.json"
    save_pv_system_config(_config(PvArray(kwp=1.0)), path)
    assert path.exists()
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize(
    "config, message_fragment",
    [
        (_config(), "at least one sub-array"),
        (_config(PvArray(kwp=0)), "kwp must be greater than 0"),
        (_config(PvArray(kwp=-1)), "kwp must be greater than 0"),
        (_config(PvArray(kwp=1, tilt_deg=-15)), "tilt_deg must be between 0 and 90"),
        (_config(PvArray(kwp=1, tilt_deg=91)), "tilt_deg must be between 0 and 90"),
        (_config(PvArray(kwp=1, azimuth_deg=200)), "azimuth_deg must be between"),
        (_config(PvArray(kwp=1), ratio=0), "performance_ratio"),
        (_config(PvArray(kwp=1), ratio=1.5), "performance_ratio"),
    ],
)
def test_validate_rejects_rather_than_clamping(
    config: PvSystemConfig, message_fragment: str
) -> None:
    """The write path must never silently drop or clamp — that is the read
    path's job, and doing it here would discard a value the user just typed."""
    with pytest.raises(ValueError, match=message_fragment):
        validate_pv_system(config)


def test_validate_names_the_offending_row(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"arrays\[1\]\.tilt_deg"):
        validate_pv_system(_config(PvArray(kwp=1), PvArray(kwp=1, tilt_deg=120)))


def test_save_does_not_write_an_invalid_config(tmp_path: Path) -> None:
    path = tmp_path / "pv_system.json"
    with pytest.raises(ValueError):
        save_pv_system_config(_config(PvArray(kwp=-1)), path)
    assert not path.exists()
