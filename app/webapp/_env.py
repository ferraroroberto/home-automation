"""Shared ``.env`` knob parsers for the webapp's background-task modules.

``presence_automation`` / ``presence_refresher`` / ``security_automation`` /
``automation`` / ``telemetry_sampler`` / ``routers.presence`` each read a
handful of ``os.getenv`` knobs (after ``load_dotenv``) with the same
graceful-default semantics. The two parsers used to be copy-pasted verbatim
into each module; they live here once now so the "blank → default,
invalid → warn-and-default" behaviour can't drift between background loops.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("⚠️ Invalid %s=%s; using %s", name, raw, default)
        return default
