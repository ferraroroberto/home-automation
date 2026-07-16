"""Shared paths, build identity, and router factories used by >1 router module."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple, Type, Union

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.static_versioning import BuildInfo

logger = logging.getLogger(__name__)

# app/webapp/routers/_helpers.py → parents[3] is the repo root.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
STATIC_DIR = Path(__file__).resolve().parents[1] / "static"

# Build identity, computed once at import — the tray restarts on every
# code edit, so a fresh process always reflects the deployed code.
BUILD_INFO = BuildInfo(STATIC_DIR, PROJECT_ROOT)


class DisplayNamePayload(BaseModel):
    """Body of every ``PUT /api/.../{id}/display_name`` request."""

    display_name: str


def make_display_name_endpoint(
    router: APIRouter,
    path: str,
    id_field: str,
    setter: Callable[[str, str], Any],
    *,
    log_noun: str = "display name",
    response_id: Optional[Callable[[str], Any]] = None,
) -> Callable[..., Any]:
    """Register a ``PUT {path}`` display-name handler on ``router``.

    Collapses the identical "strip → setter → on-error warn + 500 → return
    ``{id_field: id, display_name}``" shape shared by the units / tuya / network
    device rename endpoints. ``path`` carries a single ``{item_id}`` placeholder
    (its name is internal — the matched URLs are unchanged). ``log_noun`` is the
    phrase in the warning line; ``response_id`` optionally transforms the id in
    the response body (e.g. ``normalize_mac`` for the MAC-keyed endpoint).

    Endpoints whose shape genuinely differs are intentionally left inline:
    the security zone endpoint (int id), the Wi-Fi endpoint (id from the body),
    and the lights/cameras endpoints (different error path, extra ``display_key``).
    """
    transform = response_id if response_id is not None else (lambda value: value)

    async def endpoint(item_id: str, payload: DisplayNamePayload) -> Dict[str, Any]:
        name = payload.display_name.strip()
        try:
            setter(item_id, name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("⚠️  Failed to save %s for %s: %s", log_noun, item_id, exc)
            raise HTTPException(status_code=500, detail=f"failed to save display name: {exc}")
        return {id_field: transform(item_id), "display_name": name or None}

    endpoint.__name__ = f"set_{id_field}_display_name"
    router.put(path)(endpoint)
    return endpoint


ExcTypes = Union[Type[Exception], Tuple[Type[Exception], ...]]


def make_http_error_mapper(
    config_exc: ExcTypes,
    command_exc: ExcTypes,
    *,
    noun: str,
) -> Callable[[Exception], HTTPException]:
    """Build a device/service ``_http_error(exc)`` mapper.

    Collapses the identical "config error → 503, command error → 502, anything
    else → 502 + a warning log" shape duplicated across the security / lights /
    cameras / dhcp_plan routers, differing only in which exception classes each
    checks. ``noun`` names the thing being called for the fallback log/detail
    text (e.g. ``"RISCO Cloud"``, ``"Elgato lights"``).
    """

    def _http_error(exc: Exception) -> HTTPException:
        if isinstance(exc, config_exc):
            return HTTPException(status_code=503, detail=str(exc))
        if isinstance(exc, command_exc):
            return HTTPException(status_code=502, detail=str(exc))
        logger.warning("⚠️ Failed to call %s: %s", noun, exc)
        return HTTPException(status_code=502, detail=f"failed to call {noun}: {exc}")

    return _http_error


async def _json_body(request: Request) -> Dict[str, Any]:
    """Parse the request body as a JSON object, or raise 400."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="expected a JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    return body


async def _bool_field(request: Request, name: str) -> bool:
    """Read a required boolean field off the request's JSON body, or raise 400."""
    body = await _json_body(request)
    value = body.get(name)
    if not isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"'{name}' must be a boolean")
    return value


async def _str_field(request: Request, name: str) -> Optional[str]:
    """Read a required string field off the request's JSON body, or raise 400."""
    body = await _json_body(request)
    value = body.get(name)
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"'{name}' must be a string")
    return value
