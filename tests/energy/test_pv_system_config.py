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

from src._schedule_store import StoreUnreadableError
from src.pv_system_config import (
    MIN_THERMAL_PERFORMANCE_RATIO,
    PvArray,
    PvHorizonPoint,
    PvSystemConfig,
    load_pv_system_config,
    save_pv_system_config,
    thermal_migration_error,
    validate_pv_system,
)


def _write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "pv_system.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_missing_file_returns_none(tmp_path: Path) -> None:
    assert load_pv_system_config(tmp_path / "nope.json") is None


def test_malformed_json_raises_instead_of_returning_none(tmp_path: Path) -> None:
    """Issue #692: corrupt content must not look like "not configured" —
    ``update_pv_system`` would save that empty default back over a real array."""
    path = tmp_path / "pv_system.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(StoreUnreadableError):
        load_pv_system_config(path)


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
    assert set(raw) == {"_doc", "arrays", "performance_ratio", "horizon_profile"}
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


# ------------------------------ panel-temperature switch (issue #591)
# The switch reinterprets ``performance_ratio`` rather than merely adding a
# term, so the tests that matter are the ones pinning (a) that every existing
# file still reads as "off" and (b) that the half-migrated pair is refused.


def test_the_switch_is_absent_from_existing_files_and_absent_means_off(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, {"arrays": [{"kwp": 5.0}], "performance_ratio": 0.8})
    config = load_pv_system_config(path)
    assert config is not None
    assert config.thermal_model_enabled is False
    assert PvSystemConfig().thermal_model_enabled is False


def test_a_literal_true_arms_the_switch(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {
            "arrays": [{"kwp": 5.0}],
            "performance_ratio": 0.88,
            "thermal_model_enabled": True,
        },
    )
    config = load_pv_system_config(path)
    assert config is not None
    assert config.thermal_model_enabled is True


@pytest.mark.parametrize("value", ["true", "yes", 1, [1], {}])
def test_only_a_literal_true_arms_the_switch(tmp_path: Path, value: object) -> None:
    """A truthy string or 1 left in a hand-edited file must not change what the
    forecast predicts — arming the term is an explicit act, not a coincidence."""
    path = _write(
        tmp_path,
        {"arrays": [{"kwp": 5.0}], "thermal_model_enabled": value},
    )
    config = load_pv_system_config(path)
    assert config is not None
    assert config.thermal_model_enabled is False


def test_the_switch_off_reports_no_migration_error() -> None:
    assert thermal_migration_error(_config(PvArray(kwp=5.0), ratio=0.8)) is None


def test_the_switch_on_with_a_migrated_ratio_reports_no_error() -> None:
    config = _config(PvArray(kwp=5.0), ratio=0.88)
    config.thermal_model_enabled = True
    assert thermal_migration_error(config) is None
    validate_pv_system(config)  # must not raise


@pytest.mark.parametrize("ratio", [0.8, 0.75, MIN_THERMAL_PERFORMANCE_RATIO - 0.01])
def test_the_switch_on_over_an_unmigrated_ratio_is_refused(ratio: float) -> None:
    """The one combination that would double-count the thermal loss."""
    config = _config(PvArray(kwp=5.0), ratio=ratio)
    config.thermal_model_enabled = True

    message = thermal_migration_error(config)
    assert message is not None
    assert "double" in message or "twice" in message
    with pytest.raises(ValueError, match="thermal_model_enabled"):
        validate_pv_system(config)


def test_save_preserves_a_hand_set_switch_it_does_not_own(tmp_path: Path) -> None:
    """The editor has no control for the switch, so a save must leave it alone
    rather than writing the default back over a deliberate setting."""
    path = _write(
        tmp_path,
        {
            "_doc": "note",
            "arrays": [{"kwp": 5.0}],
            "performance_ratio": 0.88,
            "thermal_model_enabled": True,
        },
    )
    config = _config(PvArray(kwp=6.0), ratio=0.9)
    config.thermal_model_enabled = True
    save_pv_system_config(config, path)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["thermal_model_enabled"] is True
    assert raw["arrays"] == [{"kwp": 6.0, "tilt_deg": 30.0, "azimuth_deg": 0.0}]
    assert load_pv_system_config(path).thermal_model_enabled is True


# --------------------------- horizon/shading profile switch (issue #578 part b)
# Same shape as the panel-temperature switch's tests above: pin that every
# existing file reads as "off", that only a literal ``true`` arms it, and that
# a save preserves a hand-set switch it has no editor control for. Unlike the
# thermal switch, the *points* themselves are an owned, editor-controlled list.


def test_horizon_switch_is_absent_from_existing_files_and_absent_means_off(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, {"arrays": [{"kwp": 5.0}], "performance_ratio": 0.8})
    config = load_pv_system_config(path)
    assert config is not None
    assert config.horizon_profile_enabled is False
    assert config.horizon_profile == []
    assert PvSystemConfig().horizon_profile_enabled is False


@pytest.mark.parametrize("value", ["true", "yes", 1, [1], {}])
def test_only_a_literal_true_arms_the_horizon_switch(
    tmp_path: Path, value: object
) -> None:
    path = _write(
        tmp_path,
        {"arrays": [{"kwp": 5.0}], "horizon_profile_enabled": value},
    )
    config = load_pv_system_config(path)
    assert config is not None
    assert config.horizon_profile_enabled is False


def test_horizon_profile_loads_and_clamps_points(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {
            "arrays": [{"kwp": 5.0}],
            "horizon_profile": [
                {"azimuth_deg": 165, "elevation_deg": 5},
                {"azimuth_deg": 400, "elevation_deg": -10},  # wraps, clamps to 0
                {"azimuth_deg": 285, "elevation_deg": 120},  # clamps to 90
            ],
        },
    )
    config = load_pv_system_config(path)
    assert config is not None
    assert len(config.horizon_profile) == 3
    assert config.horizon_profile[1].azimuth_deg == 40.0
    assert config.horizon_profile[1].elevation_deg == 0.0
    assert config.horizon_profile[2].elevation_deg == 90.0


def test_a_malformed_horizon_point_is_skipped_not_fatal(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {
            "arrays": [{"kwp": 5.0}],
            "horizon_profile": [
                {"azimuth_deg": 90, "elevation_deg": 5},
                {"elevation_deg": 10},  # missing azimuth_deg — skipped
                "not an object",  # skipped
            ],
        },
    )
    config = load_pv_system_config(path)
    assert config is not None
    assert len(config.horizon_profile) == 1
    assert config.horizon_profile[0].azimuth_deg == 90.0


def test_a_non_list_horizon_profile_is_ignored_not_fatal(tmp_path: Path) -> None:
    path = _write(tmp_path, {"arrays": [{"kwp": 5.0}], "horizon_profile": "nope"})
    config = load_pv_system_config(path)
    assert config is not None
    assert config.horizon_profile == []


@pytest.mark.parametrize(
    "point, fragment",
    [
        (PvHorizonPoint(azimuth_deg=400, elevation_deg=5), "azimuth_deg"),
        (PvHorizonPoint(azimuth_deg=-1, elevation_deg=5), "azimuth_deg"),
        (PvHorizonPoint(azimuth_deg=90, elevation_deg=-1), "elevation_deg"),
        (PvHorizonPoint(azimuth_deg=90, elevation_deg=91), "elevation_deg"),
    ],
)
def test_validate_rejects_bad_horizon_points(point: PvHorizonPoint, fragment: str) -> None:
    config = _config(PvArray(kwp=1))
    config.horizon_profile = [point]
    with pytest.raises(ValueError, match=fragment):
        validate_pv_system(config)


def test_save_round_trips_the_horizon_profile(tmp_path: Path) -> None:
    path = tmp_path / "pv_system.json"
    config = _config(PvArray(kwp=5.0))
    config.horizon_profile = [
        PvHorizonPoint(azimuth_deg=165, elevation_deg=5),
        PvHorizonPoint(azimuth_deg=285, elevation_deg=20),
    ]
    save_pv_system_config(config, path)

    reloaded = load_pv_system_config(path)
    assert reloaded is not None
    assert [(p.azimuth_deg, p.elevation_deg) for p in reloaded.horizon_profile] == [
        (165.0, 5.0),
        (285.0, 20.0),
    ]


def test_save_preserves_a_hand_set_horizon_switch_it_does_not_own(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        {
            "arrays": [{"kwp": 5.0}],
            "horizon_profile": [{"azimuth_deg": 90, "elevation_deg": 10}],
            "horizon_profile_enabled": True,
        },
    )
    config = _config(PvArray(kwp=6.0))
    config.horizon_profile = [PvHorizonPoint(azimuth_deg=270, elevation_deg=15)]
    save_pv_system_config(config, path)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["horizon_profile_enabled"] is True
    assert raw["horizon_profile"] == [{"azimuth_deg": 270.0, "elevation_deg": 15.0}]
