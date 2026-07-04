"""
RISCO native WebUI HTML/JSON scraper
=====================================
The RISCO Cloud API (``pyrisco``) covers system/zone state reads and event
history, but this panel rejects pyrisco's partition-based arm/disarm ("use Arm
action") and the cloud payload omits the live ``ongoing_alarm``/``memory_alarm``
flags and an authoritative arm state for some panel configurations. This module
drives the same undocumented, screen-scraped native WebUI
(``webui.riscocloud.com``) the RISCO browser app uses to fill those gaps: site
selection + PIN login (``_SiteLoginParser``, ``_webui_login``), the arm/disarm
command path (``_webui_arm_disarm``), and a best-effort read of the panel's raw
state flags (``_webui_state_flags``).

Extracted verbatim from ``risco_client.py`` (issue #328) to keep this brittle,
screen-scraped path visually separate from the typed pyrisco Cloud fetcher —
this is the path that was the source of the false-intrusion bug fixed in #307
(a poll where the scrape comes back unreadable must read as *unknown*, not
*cleared*). That interpretation lives in ``risco_client.py`` (``_has_ongoing_alarm``
/ ``_state_from_alarm``), which merges this module's ``webui_flags`` dict with
the pyrisco status — only the HTTP/scraping mechanics moved here, not the
merge logic. This was a pure code move: no behavior changed.

``risco_client.py`` imports ``RiscoConfigError``, ``RiscoCommandError``,
``_load_credentials``, ``_webui_arm_disarm``, and ``_webui_state_flags`` from
this module; nothing here imports back from ``risco_client``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from html.parser import HTMLParser
from typing import Any, AsyncIterator, Optional, Tuple
from urllib.parse import urljoin

import aiohttp
from dotenv import load_dotenv

logger = logging.getLogger("risco")

_WEBUI_ROOT = "https://webui.riscocloud.com/"
_WEBUI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125 Safari/537.36"
    ),
}
_WEBUI_ACTION_TYPES = {
    "disarm": "disarmed",
    # Native RISCO WebUI popup command ids: Full=1, Partial=2, Perimeter=4.
    "arm": "ELArm1",
    "partial": "ELArm2",
    "perimeter": "ELArm4",
}


class RiscoConfigError(RuntimeError):
    """Raised when required RISCO credentials/config are missing."""


class RiscoCommandError(RuntimeError):
    """Raised when RISCO Cloud rejects a login or a control command."""


def _load_credentials() -> Tuple[str, str, str]:
    """Read RISCO_USERNAME / RISCO_PASSWORD / RISCO_PIN from the environment."""
    load_dotenv(override=True)
    username = (os.getenv("RISCO_USERNAME") or "").strip()
    password = (os.getenv("RISCO_PASSWORD") or "").strip()
    pin = (os.getenv("RISCO_PIN") or "").strip()
    if not username or not password or not pin:
        raise RiscoConfigError(
            "Missing credentials. Copy .env.example to .env and set "
            "RISCO_USERNAME, RISCO_PASSWORD and RISCO_PIN (your RISCO Cloud "
            "login and panel PIN)."
        )
    return username, password, pin


class _SiteLoginParser(HTMLParser):
    """Extract the first RISCO WebUI site id from the PIN-selection form."""

    def __init__(self) -> None:
        super().__init__()
        self.site_id: Optional[str] = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag != "input" or self.site_id is not None:
            return
        data = dict(attrs)
        if data.get("name") == "SelectedSiteId" and data.get("value"):
            self.site_id = str(data["value"])


@asynccontextmanager
async def _webui_session() -> AsyncIterator[aiohttp.ClientSession]:
    """Log in to the native RISCO WebUI and select the configured site."""
    username, password, pin = _load_credentials()
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout, headers=_WEBUI_HEADERS) as session:
        await _webui_login(session, username, password, pin)
        yield session


async def _webui_login(
    session: aiohttp.ClientSession,
    username: str,
    password: str,
    pin: str,
) -> None:
    root = _WEBUI_ROOT
    async with session.get(root) as response:
        await response.text()
        if response.status >= 400:
            raise RiscoCommandError(f"RISCO WebUI login page returned HTTP {response.status}.")

    async with session.post(
        root,
        data={"username": username, "password": password, "RememberMe": "false"},
        headers={"Origin": root.rstrip("/"), "Referer": root},
        allow_redirects=False,
    ) as response:
        await response.text()
        if response.status not in (301, 302, 303):
            raise RiscoCommandError(f"RISCO WebUI rejected the credentials (HTTP {response.status}).")
        site_login_url = urljoin(root, response.headers.get("Location") or "/SiteLogin/Index")

    async with session.get(site_login_url, headers={"Referer": root}) as response:
        html = await response.text(errors="replace")
        if response.status >= 400:
            raise RiscoCommandError(f"RISCO WebUI site selection returned HTTP {response.status}.")

    parser = _SiteLoginParser()
    parser.feed(html)
    site_id = parser.site_id
    if not site_id:
        # Defensive fallback for minor markup changes.
        match = re.search(
            r"name=[\"']SelectedSiteId[\"'][^>]*value=[\"']([^\"']+)",
            html,
            re.IGNORECASE,
        )
        site_id = match.group(1) if match else None
    if not site_id:
        raise RiscoCommandError("RISCO WebUI did not return a selectable alarm site.")

    async with session.post(
        urljoin(root, "/SiteLogin"),
        data={"SelectedSiteId": site_id, "Pin": pin},
        headers={"Origin": root.rstrip("/"), "Referer": site_login_url},
        allow_redirects=True,
    ) as response:
        await response.text()
        if response.status >= 400:
            raise RiscoCommandError(f"RISCO WebUI rejected the panel PIN (HTTP {response.status}).")


async def _webui_arm_disarm(action: str) -> None:
    command_type = _WEBUI_ACTION_TYPES[action]
    passcode = "" if action == "disarm" else "------"
    async with _webui_session() as session:
        async with session.post(
            urljoin(_WEBUI_ROOT, "/Security/ArmDisarm"),
            data={"type": command_type, "passcode": passcode, "bypassZoneId": "-1"},
            headers={
                "Origin": _WEBUI_ROOT.rstrip("/"),
                "Referer": urljoin(_WEBUI_ROOT, "/MainPage/MainPage"),
                "X-Requested-With": "XMLHttpRequest",
            },
        ) as response:
            if response.status >= 400:
                raise RiscoCommandError(
                    f"RISCO WebUI rejected '{action}' with HTTP {response.status}."
                )
            try:
                payload = await response.json(content_type=None)
            except Exception as exc:  # noqa: BLE001
                text = await response.text(errors="replace")
                raise RiscoCommandError(
                    f"RISCO WebUI returned a non-JSON response for '{action}': {text[:120]}"
                ) from exc
        if not isinstance(payload, dict):
            raise RiscoCommandError(f"RISCO WebUI returned an invalid response for '{action}'.")
        if payload.get("error") not in (0, "0", None):
            message = payload.get("errorMessage") or payload.get("strResult") or payload.get("message")
            raise RiscoCommandError(f"RISCO rejected '{action}': {message or payload.get('error')}")
        await _webui_wait_for_command_refresh(session, payload)
        if action == "disarm":
            await _webui_dismiss_alarm(session)


async def _webui_wait_for_command_refresh(
    session: aiohttp.ClientSession,
    payload: dict[str, Any],
) -> None:
    str_result = payload.get("strResult")
    if not isinstance(str_result, str) or not str_result.isdigit():
        return
    wait_seconds = min(int(str_result) + 4, 45)
    await asyncio.sleep(wait_seconds)
    async with session.post(
        urljoin(_WEBUI_ROOT, "/Security/ArmDisarm"),
        data={"type": "Refresh", "passcode": "------", "bypassZoneId": "-1"},
        headers={
            "Origin": _WEBUI_ROOT.rstrip("/"),
            "Referer": urljoin(_WEBUI_ROOT, "/MainPage/MainPage"),
            "X-Requested-With": "XMLHttpRequest",
        },
    ) as response:
        await response.text()


async def _webui_dismiss_alarm(session: aiohttp.ClientSession) -> None:
    """Ask WebUI to clear the memory-alarm banner after a disarm."""
    alarm_time = await _webui_latest_alarm_time(session)
    if not alarm_time:
        return
    async with session.post(
        urljoin(_WEBUI_ROOT, "/EventHistory/AlarmDismiss"),
        data={"YTime": alarm_time},
        headers={
            "Origin": _WEBUI_ROOT.rstrip("/"),
            "Referer": urljoin(_WEBUI_ROOT, "/MainPage/MainPage"),
            "X-Requested-With": "XMLHttpRequest",
        },
    ) as response:
        if response.status >= 400:
            logger.info("RISCO alarm dismiss returned HTTP %s", response.status)
            return
        try:
            payload = await response.json(content_type=None)
        except Exception:  # noqa: BLE001
            logger.info("RISCO alarm dismiss returned non-JSON response")
            return
    if isinstance(payload, dict) and payload.get("error") not in (0, "0", None):
        logger.info("RISCO alarm dismiss returned error %s", payload.get("error"))


async def _webui_latest_alarm_time(session: aiohttp.ClientSession) -> Optional[str]:
    async with session.post(
        urljoin(_WEBUI_ROOT, "/EventHistory/Get"),
        headers={
            "Referer": urljoin(_WEBUI_ROOT, "/MainPage/MainPage"),
            "X-Requested-With": "XMLHttpRequest",
        },
    ) as response:
        if response.status >= 400:
            return None
        try:
            payload = await response.json(content_type=None)
        except Exception:  # noqa: BLE001
            return None
    if not isinstance(payload, dict):
        return None
    events = payload.get("eh")
    if not isinstance(events, list):
        return None
    for group in events:
        if not isinstance(group, dict):
            continue
        alarm_time = group.get("AlarmTime")
        records = group.get("LogRecords")
        if alarm_time and any(
            isinstance(record, dict) and str(record.get("Priority") or "").lower() == "alarm"
            for record in (records if isinstance(records, list) else [])
        ):
            return str(alarm_time)
    return None


async def _webui_state_flags() -> dict[str, Any]:
    """Read WebUI state flags that pyrisco does not expose for this panel."""
    async with _webui_session() as session:
        async with session.post(
            urljoin(_WEBUI_ROOT, "/Security/GetCPState"),
            headers={
                "Referer": urljoin(_WEBUI_ROOT, "/MainPage/MainPage"),
                "X-Requested-With": "XMLHttpRequest",
            },
        ) as response:
            if response.status >= 400:
                return {}
            try:
                cp_payload = await response.json(content_type=None)
            except Exception:  # noqa: BLE001
                return {}
        refresh_payload: dict[str, Any] = {}
        async with session.post(
            urljoin(_WEBUI_ROOT, "/Security/ArmDisarm"),
            data={"type": "Refresh", "passcode": "------", "bypassZoneId": "-1"},
            headers={
                "Origin": _WEBUI_ROOT.rstrip("/"),
                "Referer": urljoin(_WEBUI_ROOT, "/MainPage/MainPage"),
                "X-Requested-With": "XMLHttpRequest",
            },
        ) as response:
            if response.status < 400:
                try:
                    maybe_refresh = await response.json(content_type=None)
                    if isinstance(maybe_refresh, dict) and maybe_refresh.get("error") in (0, "0", None):
                        refresh_payload = maybe_refresh
                except Exception:  # noqa: BLE001
                    refresh_payload = {}
    if not isinstance(cp_payload, dict) or cp_payload.get("error") not in (0, "0", None):
        return {}
    state_payload = refresh_payload or cp_payload
    part_info = {}
    overview = state_payload.get("overview")
    if not isinstance(overview, dict):
        overview = cp_payload.get("overview")
    if isinstance(overview, dict) and isinstance(overview.get("partInfo"), dict):
        part_info = overview["partInfo"]
    return {
        "ongoing_alarm": cp_payload.get("OngoingAlarm"),
        "memory_alarm": cp_payload.get("MemoryAlarm"),
        "arm_state": state_payload.get("strResult"),
        "part_info": part_info,
        "part_arm_string": state_payload.get("PartArmString") or cp_payload.get("PartArmString"),
    }
