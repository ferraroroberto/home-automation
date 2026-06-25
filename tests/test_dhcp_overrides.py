"""DHCP per-MAC category-override store (issue #176 step 3).

Reuses ``src.display_names``'s atomic load/save/set, so this only verifies the
MAC-normalising wrapper + the gitignored-file graceful default, not the atomic
write itself (covered by the display-names tests).
"""

from __future__ import annotations

from src import dhcp_overrides


def test_dhcp_overrides_roundtrip_and_normalises(tmp_path) -> None:
    path = tmp_path / "dhcp_overrides.json"
    # Mixed-case in → normalised (upper) key out, so a lookup matches any casing.
    dhcp_overrides.set_dhcp_override("aa:bb:cc:dd:ee:ff", "Cameras", path)
    assert dhcp_overrides.load_dhcp_overrides(path) == {"AA:BB:CC:DD:EE:FF": "Cameras"}
    # An empty category clears the entry (device falls back to the keyword rules).
    dhcp_overrides.set_dhcp_override("AA:BB:CC:DD:EE:FF", "", path)
    assert dhcp_overrides.load_dhcp_overrides(path) == {}


def test_dhcp_overrides_missing_file_is_empty(tmp_path) -> None:
    assert dhcp_overrides.load_dhcp_overrides(tmp_path / "nope.json") == {}
