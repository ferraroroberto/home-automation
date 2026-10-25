from __future__ import annotations

from src.alarm_scene_config import (
    ScenePairing,
    load_scene_pairings,
    pairings_for_zone,
    set_scene_pairings,
)


def test_pairing_store_normalizes_and_persists(tmp_path) -> None:
    path = tmp_path / "alarm_scene_pairings.json"

    entries = set_scene_pairings(
        [
            {
                "id": " garden barbecue ",
                "zone_id": "3",
                "camera_id": "garden",
                "preset_token": "1",
                "preset_name": "Barbecue",
                "enabled": True,
            },
            # No camera_id -> dropped.
            {"id": "bad", "zone_id": 4},
            # No numeric zone_id -> dropped.
            {"id": "alsobad", "zone_id": "x", "camera_id": "garden"},
            {
                "id": "",
                "zone_id": 5,
                "camera_id": "door",
                "enabled": False,
            },
        ],
        path=path,
    )

    assert [p.id for p in entries] == ["garden-barbecue", "pairing-4"]
    assert entries[0] == ScenePairing(
        id="garden-barbecue",
        zone_id=3,
        camera_id="garden",
        preset_token="1",
        preset_name="Barbecue",
        enabled=True,
    )
    # Blank optional fields normalise to None; absent enabled -> True, explicit
    # False preserved.
    assert entries[1].preset_token is None
    assert entries[1].preset_name is None
    assert entries[1].enabled is False
    assert load_scene_pairings(path=path) == entries


def test_pairings_for_zone_filters_by_zone_and_enabled(tmp_path) -> None:
    path = tmp_path / "alarm_scene_pairings.json"
    set_scene_pairings(
        [
            {"id": "a", "zone_id": 3, "camera_id": "garden", "preset_token": "1"},
            {"id": "b", "zone_id": 3, "camera_id": "garden", "preset_token": "2"},
            {"id": "c", "zone_id": 3, "camera_id": "side", "enabled": False},
            {"id": "d", "zone_id": 9, "camera_id": "door"},
        ],
        path=path,
    )

    matched = pairings_for_zone(3, path=path)
    assert {p.id for p in matched} == {"a", "b"}
    assert pairings_for_zone(9, path=path)[0].camera_id == "door"
    assert pairings_for_zone(99, path=path) == []


def test_load_missing_file_is_empty(tmp_path) -> None:
    assert load_scene_pairings(path=tmp_path / "nope.json") == []
