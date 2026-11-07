"""Tesla Fleet API app config and OAuth token store.

The app reads the car through the Tesla Fleet API as a registered third-party
app (see ``docs/tesla.md``). This module owns everything secret about that:

* the app's client id/secret and redirect URI, from ``.env``
  (``TESLA_CLIENT_ID``, ``TESLA_CLIENT_SECRET``, ``TESLA_REDIRECT_URI``);
* the owner's OAuth tokens, in the gitignored ``config/tesla_tokens.json``
  (``TESLA_TOKENS_PATH`` overrides it), written atomically on every refresh
  because Tesla rotates the refresh token and the old one stops working.

No token, secret or code is ever logged. The scopes requested are read-only
data scopes: no command scope is asked for, so even a leaked token cannot
drive the car.

UI-free; the sampler, the API and ``src/list_tesla.py`` share it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import aiohttp
from dotenv import load_dotenv

from src._atomic_json import write_json_atomic

logger = logging.getLogger("tesla.auth")

# The Fleet API base for Europe; Spain is served by the EU region.
DEFAULT_FLEET_BASE_URL = "https://fleet-api.prd.eu.vn.cloud.tesla.com"
AUTHORIZE_URL = "https://auth.tesla.com/oauth2/v3/authorize"
TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"  # noqa: S105 — a public endpoint, not a secret

# Read-only: device data for battery/charge/odometer, location only so the
# sampler can derive a home/away flag in memory (coordinates are never stored).
# ``offline_access`` yields the refresh token; ``openid`` is required by Tesla.
SCOPES = "openid offline_access vehicle_device_data vehicle_location"
PARTNER_SCOPES = "openid vehicle_device_data vehicle_location"

DEFAULT_TOKENS_PATH = Path(__file__).resolve().parents[2] / "config" / "tesla_tokens.json"

# Refresh this long before the access token's stated expiry, so a token never
# expires between the check and the request that uses it.
_REFRESH_MARGIN_S = 300
_TIMEOUT_S = 20

_refresh_lock = asyncio.Lock()


class TeslaAuthError(RuntimeError):
    """An auth step failed; the message never carries a token or secret."""


@dataclass(frozen=True)
class TeslaAppConfig:
    """The registered app's identity and the region it talks to."""

    client_id: str
    client_secret: str
    redirect_uri: str
    fleet_base_url: str
    tokens_path: Path


def load_app_config() -> TeslaAppConfig:
    """Read the app config from ``.env`` (blank values mean not configured)."""
    load_dotenv(override=True)
    tokens = (os.getenv("TESLA_TOKENS_PATH") or "").strip()
    return TeslaAppConfig(
        client_id=(os.getenv("TESLA_CLIENT_ID") or "").strip(),
        client_secret=(os.getenv("TESLA_CLIENT_SECRET") or "").strip(),
        redirect_uri=(os.getenv("TESLA_REDIRECT_URI") or "").strip(),
        fleet_base_url=(os.getenv("TESLA_FLEET_BASE_URL") or DEFAULT_FLEET_BASE_URL).strip().rstrip("/"),
        tokens_path=Path(tokens) if tokens else DEFAULT_TOKENS_PATH,
    )


def load_tokens(path: Path) -> Optional[Dict[str, Any]]:
    """Return the stored tokens, or ``None`` when absent or unreadable."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("⚠️ Tesla token file unreadable (%s)", type(exc).__name__)
        return None
    if not isinstance(data, dict) or not data.get("refresh_token"):
        logger.warning("⚠️ Tesla token file has no refresh token")
        return None
    return data


def is_configured(config: TeslaAppConfig) -> bool:
    """True when the app identity and an owner login are both present."""
    return bool(config.client_id and config.tokens_path.exists())


def _store_token_response(config: TeslaAppConfig, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not payload.get("access_token") or not payload.get("refresh_token"):
        raise TeslaAuthError("token response carried no access/refresh token")
    tokens = {
        "access_token": payload["access_token"],
        "refresh_token": payload["refresh_token"],
        "expires_at": int(time.time()) + int(payload.get("expires_in") or 0),
    }
    write_json_atomic(config.tokens_path, tokens)
    return tokens


async def _post_token(form: Dict[str, str]) -> Dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(TOKEN_URL, data=form) as resp:
            if resp.status != 200:
                raise TeslaAuthError(f"token endpoint returned HTTP {resp.status}")
            return await resp.json(content_type=None)


def authorize_url(config: TeslaAppConfig, state: str) -> str:
    """The URL the owner opens once to log in and grant the read scopes."""
    if not config.client_id or not config.redirect_uri:
        raise TeslaAuthError("TESLA_CLIENT_ID and TESLA_REDIRECT_URI must be set in .env")
    query = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "scope": SCOPES,
        "state": state,
        "prompt_missing_scopes": "true",
    }
    return f"{AUTHORIZE_URL}?{urlencode(query)}"


async def exchange_code(config: TeslaAppConfig, code: str) -> None:
    """Trade the one-off authorization code for tokens and store them."""
    payload = await _post_token({
        "grant_type": "authorization_code",
        "client_id": config.client_id,
        "client_secret": config.client_secret,
        "code": code,
        "audience": config.fleet_base_url,
        "redirect_uri": config.redirect_uri,
    })
    _store_token_response(config, payload)
    logger.info("✅ Tesla login stored")


async def partner_token(config: TeslaAppConfig) -> str:
    """A client-credentials token, used only for the one-off domain registration."""
    payload = await _post_token({
        "grant_type": "client_credentials",
        "client_id": config.client_id,
        "client_secret": config.client_secret,
        "scope": PARTNER_SCOPES,
        "audience": config.fleet_base_url,
    })
    token = payload.get("access_token")
    if not token:
        raise TeslaAuthError("partner token response carried no access token")
    return str(token)


async def access_token(config: TeslaAppConfig) -> str:
    """A valid access token, refreshing (and re-storing) it when near expiry.

    Raises :class:`TeslaAuthError` when there is no stored login or the refresh
    fails; the client turns that into an ``error`` reading, never a crash.
    """
    async with _refresh_lock:
        tokens = load_tokens(config.tokens_path)
        if tokens is None:
            raise TeslaAuthError("no Tesla login stored; run `python -m src.list_tesla auth-url`")
        if int(tokens.get("expires_at") or 0) - _REFRESH_MARGIN_S > time.time():
            return str(tokens["access_token"])
        payload = await _post_token({
            "grant_type": "refresh_token",
            "client_id": config.client_id,
            "refresh_token": str(tokens["refresh_token"]),
        })
        refreshed = _store_token_response(config, payload)
        logger.info("🔑 Tesla access token refreshed")
        return str(refreshed["access_token"])
