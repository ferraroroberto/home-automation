"""Presence-suite fixtures."""

from __future__ import annotations

import pytest

from src.presence import presence_client as P


@pytest.fixture(autouse=True)
def _clear_service_cache() -> None:
    """Isolate each test's session cache — module-global by design (#651) —
    plus the untrusted-WARNING latch and the pending trust renewals (#659)."""

    P._SERVICE_CACHE.clear()
    P._UNTRUSTED_WARNED.clear()
    P._PENDING_TRUST.clear()
    yield
    P._SERVICE_CACHE.clear()
    P._UNTRUSTED_WARNED.clear()
    P._PENDING_TRUST.clear()
