"""Home scenes — named multi-device chores (leave, arrive, night) run in one tap.

A scene is a fixed set of up to three steps: move every blind, switch every
running AC unit off, and set the alarm mode. The three scene ids are fixed
because the Home view draws one row each at first paint; what each one does
comes from the built-in defaults below, overridable per field in gitignored
``config/home_scenes.json`` (the committed ``…sample.json`` spells out the
defaults). The file is read on every call, so an edit applies without a
restart. A field with an invalid value keeps its default and is logged —
a typo must never make a scene do something other than what it says.

UI-free: running the steps is the router's job (``app.webapp.routers.scenes``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Optional

from src._schedule_store import StoreUnreadableError, read_json

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
HOME_SCENES_PATH = _CONFIG_DIR / "home_scenes.json"

SCENE_IDS = ("leave", "arrive", "night")
# The blind and alarm vocabularies of src.devices.blind_automation and
# src.security.risco_client, restated so this store stays import-light.
BLIND_ACTIONS = ("open", "close")
ALARM_ACTIONS = ("disarm", "arm", "partial", "perimeter")

_BLIND_LABELS = {"open": "Blinds up", "close": "Blinds down"}
_ALARM_LABELS = {
    "disarm": "Alarm off",
    "arm": "Alarm full",
    "partial": "Alarm partial",
    "perimeter": "Alarm perimeter",
}


@dataclass(frozen=True)
class Scene:
    """One scene's steps; ``None`` / ``False`` leaves that device family alone."""

    id: str
    label: str
    blinds: Optional[str] = None
    ac_off: bool = False
    alarm: Optional[str] = None

    def step_labels(self) -> List[str]:
        """The steps in run order, as short phrases for the Home row's meta line."""
        labels: List[str] = []
        if self.blinds:
            labels.append(_BLIND_LABELS[self.blinds])
        if self.ac_off:
            labels.append("AC off")
        if self.alarm:
            labels.append(_ALARM_LABELS[self.alarm])
        return labels


# Leaving: close up, stop cooling an empty house, arm fully. Arriving: open up
# and disarm. Night: close up and arm the perimeter so people inside can move.
DEFAULT_SCENES: Dict[str, Scene] = {
    "leave": Scene("leave", "Leave", blinds="close", ac_off=True, alarm="arm"),
    "arrive": Scene("arrive", "Arrive", blinds="open", ac_off=False, alarm="disarm"),
    "night": Scene("night", "Night", blinds="close", ac_off=False, alarm="perimeter"),
}


def _choice(scene_id: str, key: str, raw: Dict[str, Any], allowed: tuple, default: Optional[str]) -> Optional[str]:
    if key not in raw:
        return default
    value = raw[key]
    if value is None or value == "":
        return None
    if value in allowed:
        return value
    logger.warning(
        "⚠️ config/home_scenes.json: %s.%s=%r is not one of %s — keeping the default",
        scene_id, key, value, ", ".join(allowed),
    )
    return default


def _apply_override(scene: Scene, raw: Any) -> Scene:
    if not isinstance(raw, dict):
        logger.warning("⚠️ config/home_scenes.json: %s is not an object — keeping the default", scene.id)
        return scene
    ac_off = scene.ac_off
    if "ac_off" in raw:
        if isinstance(raw["ac_off"], bool):
            ac_off = raw["ac_off"]
        else:
            logger.warning("⚠️ config/home_scenes.json: %s.ac_off is not true/false — keeping the default", scene.id)
    label = raw.get("label")
    return replace(
        scene,
        label=label.strip() if isinstance(label, str) and label.strip() else scene.label,
        blinds=_choice(scene.id, "blinds", raw, BLIND_ACTIONS, scene.blinds),
        ac_off=ac_off,
        alarm=_choice(scene.id, "alarm", raw, ALARM_ACTIONS, scene.alarm),
    )


def load_scenes(path: Optional[Path] = None) -> List[Scene]:
    """The three scenes in display order, defaults overlaid with the config file.

    An absent or unreadable file yields the defaults (an unreadable one is
    logged): the Home rows must always render and always do what they say.
    """
    target = Path(path) if path is not None else HOME_SCENES_PATH
    try:
        raw = read_json(target, None)
    except StoreUnreadableError as exc:
        logger.warning("⚠️ config/home_scenes.json unreadable, using the default scenes: %s", exc)
        raw = None
    if raw is not None and not isinstance(raw, dict):
        logger.warning("⚠️ config/home_scenes.json is not an object — using the default scenes")
        raw = None
    scenes = []
    for scene_id in SCENE_IDS:
        scene = DEFAULT_SCENES[scene_id]
        if raw and scene_id in raw:
            scene = _apply_override(scene, raw[scene_id])
        scenes.append(scene)
    return scenes


def get_scene(scene_id: str, path: Optional[Path] = None) -> Optional[Scene]:
    """One scene by id, or ``None`` for an unknown id."""
    for scene in load_scenes(path):
        if scene.id == scene_id:
            return scene
    return None
