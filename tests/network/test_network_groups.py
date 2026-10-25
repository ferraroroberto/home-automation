"""Device-group store tests (issue #513) — assignment, rename, delete."""

from __future__ import annotations

from src.network_groups import (
    delete_network_group,
    load_network_groups,
    rename_network_group,
    set_network_group,
)


def test_group_assignment_normalizes_and_clears(tmp_path) -> None:
    store = tmp_path / "network_groups.json"

    set_network_group("aa:bb:cc:dd:ee:ff", "  Elgato lights  ", path=store)
    set_network_group("A4:CF:12:11:22:33", "Garaje", path=store)
    assert load_network_groups(store) == {
        "AA:BB:CC:DD:EE:FF": "Elgato lights",
        "A4:CF:12:11:22:33": "Garaje",
    }

    # Clearing is how a device returns to the synthetic Unclassified bucket.
    set_network_group("AA:BB:CC:DD:EE:FF", "", path=store)
    assert load_network_groups(store) == {"A4:CF:12:11:22:33": "Garaje"}


def test_missing_store_is_not_an_error(tmp_path) -> None:
    assert load_network_groups(tmp_path / "absent.json") == {}


def test_rename_moves_every_member_and_merges_onto_an_existing_group(tmp_path) -> None:
    store = tmp_path / "network_groups.json"
    set_network_group("AA:00:00:00:00:01", "Lights", path=store)
    set_network_group("AA:00:00:00:00:02", "Lights", path=store)
    set_network_group("AA:00:00:00:00:03", "Camaras", path=store)

    assert rename_network_group("Lights", "Elgato lights", path=store) == 2
    assert load_network_groups(store) == {
        "AA:00:00:00:00:01": "Elgato lights",
        "AA:00:00:00:00:02": "Elgato lights",
        "AA:00:00:00:00:03": "Camaras",
    }

    # Renaming onto a name already in use merges the two groups.
    assert rename_network_group("Camaras", "Elgato lights", path=store) == 1
    assert set(load_network_groups(store).values()) == {"Elgato lights"}

    # No-ops: unknown group, same name, empty target.
    assert rename_network_group("Nope", "Whatever", path=store) == 0
    assert rename_network_group("Elgato lights", "Elgato lights", path=store) == 0
    assert rename_network_group("Elgato lights", "   ", path=store) == 0
    assert len(load_network_groups(store)) == 3


def test_delete_drops_only_the_assignments(tmp_path) -> None:
    store = tmp_path / "network_groups.json"
    set_network_group("AA:00:00:00:00:01", "Garaje", path=store)
    set_network_group("AA:00:00:00:00:02", "Garaje", path=store)
    set_network_group("AA:00:00:00:00:03", "Alexa", path=store)

    assert delete_network_group("Garaje", path=store) == 2
    # The two devices are not lost — they simply no longer have a group, which
    # is what puts them under Unclassified.
    assert load_network_groups(store) == {"AA:00:00:00:00:03": "Alexa"}

    assert delete_network_group("Garaje", path=store) == 0
    assert delete_network_group("  ", path=store) == 0
