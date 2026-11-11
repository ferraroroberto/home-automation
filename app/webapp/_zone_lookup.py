"""Shared RISCO zone id -> display name lookup.

``alarm_scene_automation.py`` and ``security_override_automation.py`` each
carried a byte-identical ``_zone_name_for`` — this centralizes it, following
the same cross-module-helper precedent as ``app/webapp/_env.py``.
"""

from __future__ import annotations


def _zone_name_for(zone_id: int, security: object) -> str:
    """Look up a zone's display name from the live zone list (id->name only).

    Only the id->name mapping is used here — the zone list's *metadata* is
    stable, unlike its momentary ``triggered`` flag, which is what makes a
    per-poll zone resolution racy.
    """

    for zone in getattr(security, "zones", None) or []:
        if int(getattr(zone, "id", -1)) == zone_id:
            return str(getattr(zone, "name", "") or zone_id)
    return str(zone_id)
