"""Shared ``.env`` knob parsers for ``src/`` and the webapp.

Every background loop and client reads a handful of ``os.getenv`` knobs (after
``load_dotenv``) with the same graceful-default semantics: blank → default,
invalid → warn-and-default. They live here once so that behaviour can't drift
between modules; ``app/webapp/_env.py`` re-exports them for the webapp.
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


def _env_int(name: str, default: int, *, non_negative: bool = False) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("⚠️ Invalid %s=%s; using %s", name, raw, default)
        return default
    if non_negative and value < 0:
        logger.warning("⚠️ %s must not be negative (%s); using %s", name, value, default)
        return default
    return value


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("⚠️ Invalid %s=%s; using %s", name, raw, default)
        return default
