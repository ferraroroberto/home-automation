"""Unit tests for the RISCO cloud-cache fallback in ``fetch_security_state`` (#98).

When the *live* panel read returns a non-retryable "panel unreachable" code such
as 26, ``fetch_security_state`` must fall back to the cloud-cached snapshot
(``fromControlPanel=False``) instead of leaking the raw error and blacking out
the whole Security tab. Both reads failing must surface a clean
``RiscoCommandError`` — never the raw response dict.

No network: ``_connect`` and ``_webui_state_flags`` are monkeypatched, and the
coroutines are driven with ``asyncio.run`` so the suite needs no async plugin.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from pyrisco.common import OperationError

import src.risco_client as rc

# A representative cloud-cached status payload: the panel is unreachable live,
# but the cache still carries the zones and the system trouble/AC flags.
_CACHED_STATUS = {
    "systemStatus": 0,
    "trouble": True,
    "acLost": False,
    "partitions": [
        {"id": 0, "armedState": 1, "alarmState": 0, "exitDelayTO": 0},
    ],
    "zones": [
        {"zoneID": 1, "zoneName": "Front door", "zoneType": 1, "status": 0},
        {"zoneID": 2, "zoneName": "Hall", "zoneType": 1, "status": 0},
    ],
}

# The exact shape RISCO returns on a result-26 live read (status 200, no text).
_RESULT_26 = (
    "{'result': 26, 'validationErrors': None, 'errorText': None, "
    "'errorTextCodeID': None, 'status': 200, 'response': None}"
)


class _FakeRisco:
    """Stand-in for ``RiscoCloud``: live read 26-fails, cached read configurable."""

    def __init__(self, cache_error: Exception | None = None) -> None:
        self._site_id = 42
        self._session_id = "sess"
        self._cache_error = cache_error
        self.cache_calls = 0

    async def get_state(self) -> object:
        raise OperationError(_RESULT_26)

    async def _authenticated_post(self, url: str, body: dict) -> dict:
        self.cache_calls += 1
        # The fallback must explicitly ask for the cloud cache, not the panel.
        assert body.get("fromControlPanel") is False
        if self._cache_error is not None:
            raise self._cache_error
        return {"state": {"status": _CACHED_STATUS}}


def _patch(monkeypatch: pytest.MonkeyPatch, risco: _FakeRisco) -> None:
    @contextlib.asynccontextmanager
    async def _fake_connect():
        yield risco

    async def _no_webui_flags() -> dict:
        return {}

    monkeypatch.setattr(rc, "_connect", _fake_connect)
    monkeypatch.setattr(rc, "_webui_state_flags", _no_webui_flags)


def test_live_26_falls_back_to_cloud_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    risco = _FakeRisco()
    _patch(monkeypatch, risco)

    state = asyncio.run(rc.fetch_security_state())

    assert state.reachable is True
    assert state.assumed_control_panel_state is True  # flagged as a cached read
    assert len(state.zones) == 2
    assert state.trouble is True
    assert "disarm" in state.supported_actions  # controls remain actionable
    assert risco.cache_calls == 1


def test_both_reads_failing_raises_clean_error(monkeypatch: pytest.MonkeyPatch) -> None:
    risco = _FakeRisco(cache_error=OperationError(_RESULT_26))
    _patch(monkeypatch, risco)

    with pytest.raises(rc.RiscoCommandError) as excinfo:
        asyncio.run(rc.fetch_security_state())

    message = str(excinfo.value)
    assert message.startswith("RISCO panel is temporarily unreachable")
    # The clean message must not be a bare raw response dict.
    assert not message.startswith("{")
