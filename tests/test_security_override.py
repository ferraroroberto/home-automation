from __future__ import annotations

from src.security_override import (
    OverrideEntry,
    load_overrides,
    override_for_zone,
    set_overrides,
)


def test_override_store_normalizes_clamps_and_persists(tmp_path) -> None:
    path = tmp_path / "security_override.json"

    entries = set_overrides(
        [
            {"id": " puerta jardin ", "zone_id": "12", "max_retries": 2, "enabled": True},
            # max_retries out of [1,3] range -> clamped, not dropped.
            {"id": "cocina", "zone_id": 21, "max_retries": 9},
            # No numeric zone_id -> dropped.
            {"id": "bad", "zone_id": "x", "max_retries": 1},
            # No max_retries at all -> dropped.
            {"id": "alsobad", "zone_id": 5},
            {"id": "", "zone_id": 7, "max_retries": 1, "enabled": False},
        ],
        path=path,
    )

    assert [e.id for e in entries] == ["puerta-jardin", "cocina", "override-5"]
    assert entries[0] == OverrideEntry(id="puerta-jardin", zone_id=12, max_retries=2, enabled=True)
    assert entries[1].max_retries == 3  # clamped down from 9
    assert entries[2].enabled is False
    assert load_overrides(path=path) == entries


def test_override_for_zone_filters_by_zone_and_enabled(tmp_path) -> None:
    path = tmp_path / "security_override.json"
    set_overrides(
        [
            {"id": "a", "zone_id": 12, "max_retries": 1},
            {"id": "b", "zone_id": 21, "max_retries": 2, "enabled": False},
        ],
        path=path,
    )

    assert override_for_zone(12, path=path).max_retries == 1
    assert override_for_zone(21, path=path) is None  # disabled
    assert override_for_zone(99, path=path) is None  # not configured


def test_load_missing_file_is_empty(tmp_path) -> None:
    assert load_overrides(path=tmp_path / "nope.json") == []
