"""Alarm scene-capture pairings CRUD over ``src.alarm_scene_config``.

Split out of ``routers/security.py`` (issue #346) — same rationale as
``security_schedules.py``: the detector→camera+preset pairing list (issue
#162) is fully self-contained and shares no state with the live RISCO
read/write path that stays in ``security.py``. The GET/PUT shape is the
shared ``make_list_crud_router`` factory (issue #571).
"""

from __future__ import annotations

from app.webapp.routers._helpers import make_list_crud_router
from src.alarm_scene_config import load_scene_pairings, set_scene_pairings

router = make_list_crud_router(
    load_scene_pairings,
    set_scene_pairings,
    path="/api/security/scene-pairings",
    noun="scene pairings",
    slug="scene_pairings",
    doc="Return the detector→camera+preset alarm-scene capture pairings (issue #162).",
)
