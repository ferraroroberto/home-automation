"""Persisted detector→camera+preset pairings for alarm scene capture (issue #162).

When the RISCO alarm trips, only the cameras *paired* to the detector that
fired are snapshotted and sent for AI analysis — a random detector firing must
not photograph the whole house. The browser edits a single list of pairings; the
webapp-owned alarm-scene orchestrator (``app/webapp/alarm_scene_automation.py``)
loads that same list and resolves the tripped zone(s) to their cameras.

One detector may have several pairings (e.g. garden camera at preset "barbecue"
*and* "lawn"). A pairing with a ``preset_token`` moves the camera to that PTZ
position before grabbing the frame; without one it snapshots wherever the lens
already points.

Stored at gitignored ``config/alarm_scene_pairings.json`` (the repo is public —
zone ids, camera ids and positions are home-identifying); the committed
``…sample.json`` is the template. Atomic temp-file + ``os.replace`` write, the
same load/save shape as ``security_schedules`` / ``display_names``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
PAIRINGS_PATH = _CONFIG_DIR / "alarm_scene_pairings.json"


@dataclass(frozen=True)
class ScenePairing:
    """One detector→camera(+preset) capture pairing."""

    id: str
    zone_id: int
    camera_id: str
    preset_token: Optional[str] = None
    preset_name: Optional[str] = None
    enabled: bool = True


def _read_json(path: Path) -> Any:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("⚠️ Could not read %s (%s); returning empty", path, exc)
        return []


def _save(path: Path, data: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    logger.info("💾 Saved %s", path)


def _safe_id(value: Any, fallback: str) -> str:
    raw = str(value or fallback).strip()
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-")
    return safe or fallback


def _opt_str(value: Any) -> Optional[str]:
    raw = str(value).strip() if value is not None else ""
    return raw or None


def clean_pairing(raw: dict, fallback_id: str) -> Optional[ScenePairing]:
    """Coerce untrusted JSON/API data into a pairing, or ``None`` if unusable.

    A pairing without a numeric ``zone_id`` or a non-empty ``camera_id`` is
    dropped — those two fields are the whole point of the mapping.
    """

    try:
        zone_id = int(raw.get("zone_id"))
    except (TypeError, ValueError):
        return None
    camera_id = str(raw.get("camera_id") or "").strip()
    if not camera_id:
        return None
    return ScenePairing(
        id=_safe_id(raw.get("id"), fallback_id),
        zone_id=zone_id,
        camera_id=camera_id,
        preset_token=_opt_str(raw.get("preset_token")),
        preset_name=_opt_str(raw.get("preset_name")),
        enabled=raw.get("enabled") is not False,
    )


def _clean_list(raw: Any) -> List[ScenePairing]:
    if not isinstance(raw, list):
        return []
    out: List[ScenePairing] = []
    for idx, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        pairing = clean_pairing(item, f"pairing-{idx}")
        if pairing is not None:
            out.append(pairing)
    return out


def load_scene_pairings(path: Optional[Path] = None) -> List[ScenePairing]:
    """Return the persisted pairing list, or ``[]`` if absent/unreadable."""

    target = Path(path) if path is not None else PAIRINGS_PATH
    return _clean_list(_read_json(target))


def save_scene_pairings(
    pairings: List[ScenePairing], path: Optional[Path] = None
) -> None:
    """Atomically persist the whole pairing list."""

    target = Path(path) if path is not None else PAIRINGS_PATH
    _save(target, [asdict(pairing) for pairing in pairings])


def set_scene_pairings(
    raw_entries: List[dict], path: Optional[Path] = None
) -> List[ScenePairing]:
    """Replace the pairing list with normalized entries and return it."""

    pairings = _clean_list(raw_entries)
    save_scene_pairings(pairings, path)
    return pairings


def pairings_for_zone(
    zone_id: int, path: Optional[Path] = None
) -> List[ScenePairing]:
    """Return the enabled pairings whose detector matches ``zone_id``."""

    return [
        pairing
        for pairing in load_scene_pairings(path)
        if pairing.enabled and pairing.zone_id == zone_id
    ]
