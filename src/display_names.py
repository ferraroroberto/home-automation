"""Local display-name overrides for MELCloud units.

Maps ``unit_id`` → ``display_name``. The real file is gitignored (it
would expose room names in a public repo). A missing file is not an
error — returns an empty dict, same "graceful default" pattern as
``webapp_config.py``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "display_names.json"


def load_display_names(path: Optional[Path] = None) -> Dict[str, str]:
    """Return {unit_id: display_name} from the config file, or {} if absent."""
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


def save_display_names(names: Dict[str, str], path: Optional[Path] = None) -> None:
    """Atomically write the display-name map to disk."""
    target = Path(path) if path is not None else DEFAULT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(names, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)
    logger.info("💾 Saved display_names to %s", target)


def set_display_name(unit_id: str, display_name: str, path: Optional[Path] = None) -> None:
    """Set or clear a single unit's display-name override, persisting immediately."""
    names = load_display_names(path)
    if display_name:
        names[unit_id] = display_name
    else:
        names.pop(unit_id, None)
    save_display_names(names, path)
