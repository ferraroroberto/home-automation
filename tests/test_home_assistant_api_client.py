from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

_API_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "home_automation_app" / "api.py"
_SPEC = importlib.util.spec_from_file_location("home_automation_app_api", _API_PATH)
assert _SPEC and _SPEC.loader
_api_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_api_module)
HomeAutomationApi = _api_module.HomeAutomationApi
HomeAutomationApiError = _api_module.HomeAutomationApiError


class _Response:
    def __init__(self, status: int, body: object) -> None:
        self.status = status
        self._body = body

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def text(self) -> str:
        return str(self._body)

    async def json(self, content_type: object = None) -> object:
        return self._body


class _Session:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def request(self, method: str, url: str, **kwargs: object) -> _Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.response


def test_api_client_sends_bearer_and_decodes_lists() -> None:
    async def run() -> None:
        session = _Session(_Response(200, {"units": [{"unit_id": "abc"}]}))
        api = HomeAutomationApi(
            session, "https://ha-pc.example:8447", "secret", verify_ssl=False
        )

        units = await api.units()

        assert units == [{"unit_id": "abc"}]
        assert session.calls[0]["method"] == "GET"
        assert session.calls[0]["url"] == "https://ha-pc.example:8447/api/units"
        assert session.calls[0]["headers"] == {"Authorization": "Bearer secret"}
        assert session.calls[0]["ssl"] is False

    asyncio.run(run())


def test_api_client_accepts_existing_bearer_secret_value() -> None:
    async def run() -> None:
        session = _Session(_Response(200, {"units": []}))
        api = HomeAutomationApi(session, "http://app.local", "Bearer existing")

        await api.units()

        assert session.calls[0]["headers"] == {"Authorization": "Bearer existing"}

    asyncio.run(run())


def test_api_client_control_security_reuses_common_request_logic() -> None:
    async def run() -> None:
        session = _Session(_Response(200, {"mode": "perimeter"}))
        api = HomeAutomationApi(session, "http://app.local", "")

        payload = await api.control_security("perimeter")

        assert payload == {"mode": "perimeter"}
        assert session.calls[0]["method"] == "POST"
        assert session.calls[0]["url"] == "http://app.local/api/security/perimeter"
        assert session.calls[0]["headers"] == {"X-Automation-Source": "ha"}

    asyncio.run(run())


def test_api_client_control_security_tags_source_alongside_bearer() -> None:
    """issue #405 — the app's alarm.jsonl distinguishes this integration's calls."""

    async def run() -> None:
        session = _Session(_Response(200, {"mode": "armed"}))
        api = HomeAutomationApi(session, "https://ha-pc.example:8447", "secret")

        await api.control_security("arm")

        assert session.calls[0]["headers"] == {
            "Authorization": "Bearer secret",
            "X-Automation-Source": "ha",
        }

    asyncio.run(run())


def test_api_client_raises_distinct_http_error() -> None:
    async def run() -> None:
        session = _Session(_Response(502, "panel refused"))
        api = HomeAutomationApi(session, "http://app.local")

        with pytest.raises(HomeAutomationApiError, match="HTTP 502"):
            await api.control_security("arm")

    asyncio.run(run())
