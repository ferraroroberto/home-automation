"""iCloud browser-trust renewal flow over ``src.presence_client``.

Split out of ``routers/presence.py`` (issue #702) — the two-step, attended
2FA renewal (issue #659) is fully self-contained and shares no state with the
rest of the presence router beyond triggering a diagnostics refresh on
success. Two-step: begin makes Apple push a 2FA code to the account's trusted
devices; complete verifies it on the SAME live pyicloud session (see
``src.presence_client``) and swaps the trusted session into the tray's cache
— no restart, no CLI. Same auth as every ``/api/*`` route.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.webapp.presence_refresher import refresh_once
from src.presence_client import (
    PresenceConfig,
    PresenceConfigError,
    TrustRenewalState,
    begin_trust_renewal,
    complete_trust_renewal,
    load_presence_configs,
)

router = APIRouter()

_TRUST_CODE_RE = re.compile(r"^\d{6}$")


def _icloud_account(account: str) -> PresenceConfig:
    """Resolve a 1-based account label to its config, or 404."""

    try:
        configs = load_presence_configs()
    except PresenceConfigError as exc:
        raise HTTPException(status_code=404, detail=f"No iCloud account configured: {exc}") from exc
    for config in configs:
        if config.label == account:
            return config
    known = ", ".join(config.label for config in configs)
    raise HTTPException(
        status_code=404, detail=f"Unknown iCloud account {account!r}; configured: {known}"
    )


def _trust_payload(config: PresenceConfig, state: TrustRenewalState) -> Dict[str, Any]:
    return {
        "account": config.label,
        "display_name": config.display_name,
        "status": state.status,
        "detail": state.detail,
        "trusted": state.trusted,
    }


@router.post("/api/presence/icloud/{account}/trust/begin")
async def post_icloud_trust_begin(account: str) -> Dict[str, Any]:
    """Ask Apple to push a 2FA code for this account (``code_sent`` /
    ``already_trusted`` / ``failed``); the code is entered via ``…/complete``."""

    config = _icloud_account(account)
    state = await asyncio.to_thread(begin_trust_renewal, config)
    if state.status == "already_trusted":
        # The fresh trusted session was adopted; let the diagnostics say so now.
        await refresh_once()
    return _trust_payload(config, state)


class TrustCodePayload(BaseModel):
    code: str


@router.post("/api/presence/icloud/{account}/trust/complete")
async def post_icloud_trust_complete(account: str, payload: TrustCodePayload) -> Dict[str, Any]:
    """Verify the pushed 6-digit code (``trusted`` / ``invalid_code`` /
    ``expired`` / ``failed``). Never logs the code."""

    config = _icloud_account(account)
    code = payload.code.replace(" ", "").strip()
    if not _TRUST_CODE_RE.match(code):
        raise HTTPException(status_code=400, detail="code must be exactly 6 digits")
    state = await asyncio.to_thread(complete_trust_renewal, config, code)
    if state.status == "trusted":
        # Re-poll on the newly trusted session so diagnostics.accounts[].trusted
        # flips without waiting for the next background refresh.
        await refresh_once()
    return _trust_payload(config, state)
