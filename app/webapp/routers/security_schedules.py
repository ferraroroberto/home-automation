"""Weekly alarm-schedule CRUD over ``src.security_schedules``.

Split out of ``routers/security.py`` (issue #346) — the same "split a grown
router by self-contained concern" move ``dhcp_plan.py`` made out of
``network.py`` in #328. The weekly schedule list is fully self-contained
(schema + persistence in :mod:`src.security_schedules`) and shares no state
with the live RISCO read/write path that stays in ``security.py``.

The GET/PUT shape itself is the shared ``make_list_crud_router`` factory
(issue #571) — byte-identical here, in ``security_scene.py`` and in
``security_override.py`` apart from the store and the nouns.
"""

from __future__ import annotations

from app.webapp.routers._helpers import make_list_crud_router
from src.security_schedules import load_security_schedules, set_security_schedules

router = make_list_crud_router(
    load_security_schedules,
    set_security_schedules,
    path="/api/security/schedules",
    noun="schedules",
    log_noun="alarm schedules",
    slug="security_schedules",
    doc="Return the weekly alarm arm/disarm schedule entries.",
)
