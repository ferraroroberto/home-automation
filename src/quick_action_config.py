"""Stream-Deck quick-action device bindings (issue #641, redacted in #831).

``app.webapp.actions_registry`` used to hardcode the Tuya plug device id and
the Home-Assistant climate entity bound to the ``plug_on``/``plug_off``/
``ac_on``/``ac_off`` quick actions directly in source — both values carry a
real room name, which is not fit for this public repo's tracked Python.
Moved here instead: persisted at gitignored ``config/quick_actions.json``
(the committed ``…sample.json`` carries empty placeholders), loaded once at
import time the same way ``src.webapp_config`` loads its own settings.

There is still no UI for re-pointing an action — edit the JSON file directly
if the bound device/entity changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src._schedule_store import read_json

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
QUICK_ACTIONS_PATH = _CONFIG_DIR / "quick_actions.json"


class QuickActionConfigError(RuntimeError):
    """A quick action was invoked with no device/entity bound in config."""


@dataclass(frozen=True)
class QuickActionsConfig:
    """User-authored device bindings for the Stream Deck quick actions."""

    plug_device_id: str = ""
    ac_climate_entity: str = ""


def load_quick_actions_config(path: Optional[Path] = None) -> QuickActionsConfig:
    """Load the quick-actions config, falling back to empty (unbound) if absent."""
    target = Path(path) if path is not None else QUICK_ACTIONS_PATH
    raw = read_json(target, None)
    if raw is None:
        return QuickActionsConfig()
    return QuickActionsConfig(
        plug_device_id=str(raw.get("plug_device_id") or ""),
        ac_climate_entity=str(raw.get("ac_climate_entity") or ""),
    )


def require_plug_device_id(cfg: QuickActionsConfig) -> str:
    """Return the bound plug device id, or raise if unconfigured."""
    if not cfg.plug_device_id:
        raise QuickActionConfigError(
            "plug_device_id is not set — add it to config/quick_actions.json "
            "(see config/quick_actions.sample.json)."
        )
    return cfg.plug_device_id


def require_ac_climate_entity(cfg: QuickActionsConfig) -> str:
    """Return the bound climate entity id, or raise if unconfigured."""
    if not cfg.ac_climate_entity:
        raise QuickActionConfigError(
            "ac_climate_entity is not set — add it to config/quick_actions.json "
            "(see config/quick_actions.sample.json)."
        )
    return cfg.ac_climate_entity
