"""Async client for this repo's FastAPI backend.

The Home Assistant integration is intentionally a thin adapter: every read and
write goes through the existing `/api/*` endpoints used by the PWA. No MELCloud,
Tuya, RISCO, or SMA business logic is duplicated here.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import aiohttp


class HomeAutomationApiError(RuntimeError):
    """Raised when the app API cannot be reached or rejects a request."""


class HomeAutomationApi:
    """Small reusable API wrapper for all HA entity platforms."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        token: str | None = None,
        verify_ssl: bool = True,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/") + "/"
        self._token = (token or "").strip()
        self._verify_ssl = verify_ssl

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call one app API endpoint and return its JSON object body."""
        headers = {}
        if self._token:
            headers["Authorization"] = (
                self._token if self._token.lower().startswith("bearer ") else f"Bearer {self._token}"
            )
        url = urljoin(self._base_url, path.lstrip("/"))
        try:
            async with self._session.request(
                method,
                url,
                json=json,
                headers=headers,
                ssl=self._verify_ssl,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                text = await response.text()
                if response.status >= 400:
                    raise HomeAutomationApiError(
                        f"{method} {path} failed with HTTP {response.status}: {text}"
                    )
                data = await response.json(content_type=None)
        except HomeAutomationApiError:
            raise
        except Exception as exc:  # noqa: BLE001 - HA surfaces this in entity logs
            raise HomeAutomationApiError(f"{method} {path} failed: {exc}") from exc
        if not isinstance(data, dict):
            raise HomeAutomationApiError(f"{method} {path} returned a non-object JSON body")
        return data

    async def units(self) -> list[dict[str, Any]]:
        return list((await self.request("GET", "/api/units")).get("units") or [])

    async def control_unit(self, unit_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.request("POST", f"/api/units/{unit_id}", json=payload)

    async def tuya_devices(self) -> list[dict[str, Any]]:
        return list((await self.request("GET", "/api/tuya")).get("devices") or [])

    async def set_tuya_switch(self, device_id: str, on: bool) -> dict[str, Any]:
        return await self.request("POST", f"/api/tuya/{device_id}/switch", json={"on": on})

    async def security(self) -> dict[str, Any]:
        return await self.request("GET", "/api/security")

    async def control_security(self, action: str) -> dict[str, Any]:
        return await self.request("POST", f"/api/security/{action}")

    async def set_zone_bypass(self, zone_id: str | int, bypass: bool) -> dict[str, Any]:
        return await self.request(
            "POST", f"/api/security/zones/{zone_id}/bypass", json={"bypass": bypass}
        )

    async def energy(self) -> dict[str, Any]:
        return await self.request("GET", "/api/energy")
