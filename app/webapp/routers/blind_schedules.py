"""Daily blind schedule CRUD over ``src.blind_automation`` (issue #871).

The same GET/PUT list shape as the alarm schedules (the shared
``make_list_crud_router`` factory, issue #571): the browser edits one list
and the background engine in ``app.webapp.blind_schedules`` fires it.
"""

from __future__ import annotations

from app.webapp.routers._helpers import make_list_crud_router
from src.blind_automation import load_blind_schedules, set_blind_schedules

router = make_list_crud_router(
    load_blind_schedules,
    set_blind_schedules,
    path="/api/blinds/schedules",
    noun="schedules",
    log_noun="blind schedules",
    slug="blind_schedules",
    doc="Return the daily blind up/down schedule entries.",
)
