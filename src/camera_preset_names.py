"""Local display-name overrides for camera PTZ presets (issue #212).

Maps ``"<camera_id>::<preset_token>"`` → ``display_name``. Lets a user rename a
preset (e.g. "Position 1" → "Front door") without re-saving it on the camera,
which would move the lens. The real file is gitignored; a missing file is not an
error — same graceful-default pattern as ``display_names.py`` /
``camera_display_names.py``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Optional

from src._atomic_json import write_json_atomic

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "camera_preset_names.json"


def _key(camera_id: str, token: str) -> str:
    return f"{camera_id}::{token}"


def load_preset_names(path: Optional[Path] = None) -> Dict[str, str]:
    """Return {"camera_id::token": name} from the config file, or {} if absent."""
    target = Path(path) if path is not None else DEFAULT_PATH
    if not target.exists():
        return {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("⚠️ Could not read %s (%s); returning empty overrides", target, exc)
        return {}
    if not isinstance(raw, dict):
        logger.warning("⚠️ %s is not a JSON object; returning empty overrides", target)
        return {}
    return {str(k): str(v) for k, v in raw.items() if v}


def save_preset_names(names: Dict[str, str], path: Optional[Path] = None) -> None:
    """Atomically write the preset-name map to disk."""
    target = Path(path) if path is not None else DEFAULT_PATH
    write_json_atomic(target, names)
    logger.info("💾 Saved camera_preset_names to %s", target)


def set_preset_name(
    camera_id: str, token: str, display_name: str, path: Optional[Path] = None
) -> None:
    """Set or clear one preset's name override, persisting immediately."""
    names = load_preset_names(path)
    key = _key(camera_id, token)
    if display_name:
        names[key] = display_name
    else:
        names.pop(key, None)
    save_preset_names(names, path)


def apply_overrides(
    camera_id: str, presets: list, path: Optional[Path] = None
) -> list:
    """Overlay any name overrides onto a list of ``{token, name}`` presets."""
    names = load_preset_names(path)
    out = []
    for preset in presets:
        token = str(preset.get("token", ""))
        override = names.get(_key(camera_id, token))
        out.append({**preset, "name": override} if override else dict(preset))
    return out
