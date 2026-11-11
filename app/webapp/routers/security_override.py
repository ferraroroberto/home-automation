"""Auto-bypass-after-N-repeats detector rules CRUD over ``src.security.security_override``.

Split out of ``routers/security.py`` — same rationale as
``security_schedules.py``: the per-detector override rule list
is fully self-contained and shares no state with the live RISCO read/write
path that stays in ``security.py``. The GET/PUT shape is the shared
``make_list_crud_router`` factory.
"""

from __future__ import annotations

from app.webapp.routers._helpers import make_list_crud_router
from src.security.security_override import load_overrides, set_overrides

router = make_list_crud_router(
    load_overrides,
    set_overrides,
    path="/api/security/overrides",
    noun="overrides",
    log_noun="alarm overrides",
    slug="overrides",
    doc='Return the "auto-bypass after N repeats this session" detector rules (issue #341).',
)
