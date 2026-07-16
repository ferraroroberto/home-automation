"""Local display-name overrides for camera PTZ presets (issue #212).

Maps ``"<camera_id>::<preset_token>"`` → ``display_name``. Lets a user rename a
preset (e.g. "Position 1" → "Front door") without re-saving it on the camera,
which would move the lens. The atomic load/save logic is shared from
``src.display_names`` — only the on-disk path and the composite ``camera_id::
token`` key differ. The real file is gitignored; a missing file is not an
error — same graceful-default pattern as ``display_names.py`` /
``camera_display_names.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from src.display_names import load_display_names, save_display_names

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "camera_preset_names.json"


def _key(camera_id: str, token: str) -> str:
    return f"{camera_id}::{token}"


def set_preset_name(
    camera_id: str, token: str, display_name: str, path: Optional[Path] = None
) -> None:
    """Set or clear one preset's name override, persisting immediately."""
    target = DEFAULT_PATH if path is None else path
    names = load_display_names(target)
    key = _key(camera_id, token)
    if display_name:
        names[key] = display_name
    else:
        names.pop(key, None)
    save_display_names(names, target)


def apply_overrides(
    camera_id: str, presets: list, path: Optional[Path] = None
) -> list:
    """Overlay any name overrides onto a list of ``{token, name}`` presets."""
    names = load_display_names(DEFAULT_PATH if path is None else path)
    out = []
    for preset in presets:
        token = str(preset.get("token", ""))
        override = names.get(_key(camera_id, token))
        out.append({**preset, "name": override} if override else dict(preset))
    return out
