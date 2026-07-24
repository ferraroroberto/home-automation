"""User-defined display groups for attached network devices (issue #513).

Maps normalised ``mac`` → group name — the fifth store built on the atomic
``{key: value}`` JSON helper shared from :mod:`src.display_names`, alongside the
network rename (:mod:`src.network_display_names`) and hidden-flag
(:mod:`src.network_hidden`) stores. Only the on-disk path differs, so the group
store can never drift from how the other per-MAC overrides load, normalise, and
persist.

**Groups exist only as the set of values in this map.** There is no separate
group registry, so an empty group cannot exist: moving the last device out of a
group makes the group disappear on the next read, and a MAC that vanishes from
the LAN simply stops contributing a member (its assignment stays on disk, and
resurfaces if the device returns). Devices with no entry here fall into the
synthetic trailing "Unclassified" group the UI renders last — that group is
never persisted.

Deliberately independent of ``config/dhcp_plan.json``'s categories: those drive
IP-range assignment for DHCP reservations, these drive display only. Two systems
silently sharing one taxonomy is how the reservation drift signal gets ruined.

**A MAC is not always the device's own.** The garage NETGEAR client bridge
rewrites every downstream client's address to its own ``02:0F:B5:*`` prefix, so
those devices are grouped under an extender-derived identity that would change
if one were ever wired directly. Nothing here errors on that — an assignment
whose MAC never reappears is simply inert, and the device shows up under
Unclassified with its new address until it is re-assigned.

The real file is gitignored (it holds real MACs); ``network_groups.sample.json``
documents the shape. A missing file is not an error (empty dict).
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from src._mac import normalize_mac
from src.display_names import load_display_names, save_display_names, set_display_name

DEFAULT_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "network_groups.json"
)


def load_network_groups(path: Optional[Path] = None) -> Dict[str, str]:
    """Return ``{mac: group_name}`` from the config file, or ``{}`` if absent.

    Keys are normalised so a lookup matches regardless of the stored casing —
    the same guarantee the display-name and hidden stores give.
    """
    raw = load_display_names(DEFAULT_PATH if path is None else path)
    return {normalize_mac(k): v.strip() for k, v in raw.items() if v.strip()}


def set_network_group(mac: str, group: str, path: Optional[Path] = None) -> None:
    """Assign one device to a group, or clear it (empty name → Unclassified)."""
    set_display_name(
        normalize_mac(mac), group.strip(), DEFAULT_PATH if path is None else path
    )


def rename_network_group(
    name: str, new_name: str, path: Optional[Path] = None
) -> int:
    """Rename a group in place, returning how many devices moved.

    Renaming onto an existing group name merges the two — the natural reading of
    "call this group what that one is called", and the only outcome consistent
    with one-device-one-group. An empty ``new_name`` is rejected by the caller;
    here it would silently unclassify the members, so guard it.
    """
    target = new_name.strip()
    source = name.strip()
    if not target or not source or target == source:
        return 0
    groups = load_network_groups(path)
    moved = {mac: g for mac, g in groups.items() if g == source}
    if not moved:
        return 0
    for mac in moved:
        groups[mac] = target
    save_display_names(groups, DEFAULT_PATH if path is None else path)
    return len(moved)


def delete_network_group(name: str, path: Optional[Path] = None) -> int:
    """Drop a group, returning how many devices fell back to Unclassified.

    Only the assignments are removed — no device is ever lost, it simply stops
    having a group.
    """
    target = name.strip()
    if not target:
        return 0
    groups = load_network_groups(path)
    removed = [mac for mac, g in groups.items() if g == target]
    if not removed:
        return 0
    for mac in removed:
        groups.pop(mac, None)
    save_display_names(groups, DEFAULT_PATH if path is None else path)
    return len(removed)
