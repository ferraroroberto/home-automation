"""Pure-logic tests for the Google Directions travel-time client (issue #470).

Only the response-parsing and the no-key short-circuit are exercised — nothing
here touches the network. The endpoint-level behaviour (resolution, home lookup,
spoken fallbacks) is covered by ``tests/api/test_presence_eta.py`` with
``fetch_travel_time`` monkeypatched.
"""

from __future__ import annotations

import asyncio

import pytest

from src.travel_time import TravelTime, _parse_directions, fetch_travel_time


def test_parse_prefers_duration_in_traffic() -> None:
    data = {
        "status": "OK",
        "routes": [
            {"legs": [{
                "duration": {"value": 900, "text": "15 mins"},
                "duration_in_traffic": {"value": 1080, "text": "18 mins"},
            }]}
        ],
    }
    tt = _parse_directions(data)
    assert tt.available is True
    assert tt.duration_s == 1080  # in-traffic value wins over free-flow
    assert tt.duration_text == "18 mins"


def test_parse_falls_back_to_free_flow_duration() -> None:
    data = {"status": "OK", "routes": [{"legs": [{"duration": {"value": 600, "text": "10 mins"}}]}]}
    tt = _parse_directions(data)
    assert tt.available is True
    assert tt.duration_s == 600


def test_parse_zero_results_is_no_route() -> None:
    tt = _parse_directions({"status": "ZERO_RESULTS", "routes": []})
    assert tt.available is False
    assert tt.reason == "no_route"


@pytest.mark.parametrize("status", ["REQUEST_DENIED", "OVER_QUERY_LIMIT", "INVALID_REQUEST"])
def test_parse_other_bad_status_is_error(status: str) -> None:
    tt = _parse_directions({"status": status, "error_message": "boom"})
    assert tt.available is False
    assert tt.reason == "error"
    assert "boom" in tt.detail


def test_parse_malformed_payload_is_error_not_raise() -> None:
    assert _parse_directions({"status": "OK", "routes": []}).reason == "error"
    assert _parse_directions("nope").reason == "error"


def test_fetch_without_api_key_is_unavailable_and_makes_no_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing key short-circuits before any HTTP session is created."""
    import src.travel_time as tt

    monkeypatch.setattr(tt, "_api_key", lambda: "")

    def _boom(*_a, **_k):  # pragma: no cover - must never be reached
        raise AssertionError("no HTTP call should happen without an API key")

    monkeypatch.setattr(tt.aiohttp, "ClientSession", _boom)

    result = asyncio.run(
        fetch_travel_time(origin_lat=1.0, origin_lon=2.0, dest_lat=3.0, dest_lon=4.0)
    )
    assert result == TravelTime(available=False, reason="no_api_key")
