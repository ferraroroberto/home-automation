"""Local PTZ-preset store for cameras without native ONVIF presets (issue #190).

When a camera supports absolute moves but rejects ``GetPresets``/``SetPreset``,
saved positions are kept here as absolute coordinates and recalled via
``AbsoluteMove``. Cameras with native presets never touch this file.

Schema — ``{camera_id: [{"token", "name", "pan", "tilt", "zoom"}]}``. The real
file is gitignored (it can encode where a camera points in a public repo); a
missing file is not an error. Mirrors the atomic load/save discipline of
``display_names`` — only the shape (a nested list) differs.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from src._atomic_json import write_json_atomic

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "camera_presets.json"

Preset = Dict[str, object]


def _load(path: Optional[Path] = None) -> Dict[str, List[Preset]]:
    target = Path(path) if path is not None else DEFAULT_PATH
    if not target.exists():
        return {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("⚠️ Could not read %s (%s); returning empty presets", target, exc)
        return {}
    if not isinstance(raw, dict):
        logger.warning("⚠️ %s is not a JSON object; returning empty presets", target)
        return {}
    return {str(k): list(v) for k, v in raw.items() if isinstance(v, list)}


def _save(data: Dict[str, List[Preset]], path: Optional[Path] = None) -> None:
    target = Path(path) if path is not None else DEFAULT_PATH
    write_json_atomic(target, data)
    logger.info("💾 Saved camera presets to %s", target)


def list_local_presets(camera_id: str, path: Optional[Path] = None) -> List[Preset]:
    """Return the locally-stored presets for one camera (empty list if none)."""
    return _load(path).get(camera_id, [])


def get_local_preset(
    camera_id: str, token: str, path: Optional[Path] = None
) -> Optional[Preset]:
    """Look up one locally-stored preset by token."""
    for preset in list_local_presets(camera_id, path):
        if str(preset.get("token")) == token:
            return preset
    return None


def add_local_preset(
    camera_id: str,
    name: str,
    pan: Optional[float],
    tilt: Optional[float],
    zoom: Optional[float],
    path: Optional[Path] = None,
) -> Dict[str, str]:
    """Save a position as absolute coordinates, returning ``{token, name}``."""
    if pan is None or tilt is None:
        raise ValueError("a local preset needs a pan and tilt position")
    data = _load(path)
    presets = data.setdefault(camera_id, [])
    used = {str(p.get("token")) for p in presets}
    n = 1
    while str(n) in used:
        n += 1
    token = str(n)
    presets.append(
        {"token": token, "name": name or f"Position {token}",
         "pan": float(pan), "tilt": float(tilt),
         "zoom": float(zoom) if zoom is not None else None}
    )
    _save(data, path)
    return {"token": token, "name": name or f"Position {token}"}


def remove_local_preset(camera_id: str, token: str, path: Optional[Path] = None) -> None:
    """Delete one locally-stored preset by token, persisting immediately."""
    data = _load(path)
    presets = data.get(camera_id)
    if not presets:
        return
    data[camera_id] = [p for p in presets if str(p.get("token")) != token]
    _save(data, path)
