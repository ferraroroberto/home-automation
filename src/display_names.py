"""Local display-name overrides for MELCloud units.

Maps ``unit_id`` → ``display_name``. The real file is gitignored (it
would expose room names in a public repo). A missing file is not an
error — returns an empty dict, same "graceful default" pattern as
``webapp_config.py``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional

from src._atomic_json import write_json_atomic
from src._schedule_store import read_json

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "display_names.json"


def load_display_names(path: Optional[Path] = None) -> Dict[str, str]:
    """Return {unit_id: display_name} from the config file, or {} if absent.

    Raises :class:`~src._schedule_store.StoreUnreadableError` when the file
    exists but can't be read (issue #692) — ``set_display_name`` mutates this
    return value and saves it whole, so a transient failure here would
    otherwise get saved back over every other unit's display name.
    """
    target = Path(path) if path is not None else DEFAULT_PATH
    raw = read_json(target, None)
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if v}


def save_display_names(names: Dict[str, str], path: Optional[Path] = None) -> None:
    """Atomically write the display-name map to disk."""
    target = Path(path) if path is not None else DEFAULT_PATH
    write_json_atomic(target, names)
    logger.info("💾 Saved display_names to %s", target)


def set_display_name(unit_id: str, display_name: str, path: Optional[Path] = None) -> None:
    """Set or clear a single unit's display-name override, persisting immediately."""
    names = load_display_names(path)
    if display_name:
        names[unit_id] = display_name
    else:
        names.pop(unit_id, None)
    save_display_names(names, path)
