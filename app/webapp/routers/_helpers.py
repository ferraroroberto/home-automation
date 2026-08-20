"""Shared paths, build identity, and router factories used by >1 router module."""

from __future__ import annotations

import logging
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, Union

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.notify_config import is_notify_configured
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


def _enabled_list_payload(entries: List[Any]) -> Dict[str, Any]:
    """The ``{enabled, count, entries}`` body every self-contained list store returns."""
    active = [entry for entry in entries if getattr(entry, "enabled", False)]
    return {
        "enabled": bool(active),
        "count": len(active),
        "entries": [asdict(entry) for entry in entries],
    }


def make_list_crud_router(
    load_fn: Callable[[], List[Any]],
    set_fn: Callable[[List[Any]], List[Any]],
    *,
    path: str,
    noun: str,
    log_noun: Optional[str] = None,
    slug: Optional[str] = None,
    doc: Optional[str] = None,
) -> APIRouter:
    """Build a GET/PUT router for one self-contained, enabled-flagged list store.

    Collapses the identical shape shared by the alarm schedules / scene-pairings
    / detector-override routers: a GET that loads the list and wraps it in
    ``{enabled, count, entries}`` (500 + warning on failure), and a PUT that
    parses a JSON body, requires ``entries`` to be a list (400 otherwise), and
    persists it through ``set_fn`` (500 + warning on failure).

    ``noun`` is the phrase in the HTTP ``detail`` text ("failed to load
    {noun}"); ``log_noun`` overrides it for the warning line when the log wants
    a longer name. ``slug`` names the generated handlers (defaults to a
    slugified ``noun``); ``doc`` is the GET handler's docstring, which FastAPI
    surfaces as the endpoint description.
    """
    log_phrase = log_noun or noun
    handler_slug = slug or re.sub(r"[^a-z0-9]+", "_", noun.lower()).strip("_")

    router = APIRouter()

    async def get_entries() -> Dict[str, Any]:
        try:
            return _enabled_list_payload(load_fn())
        except Exception as exc:  # noqa: BLE001
            logger.warning("⚠️  Failed to load %s: %s", log_phrase, exc)
            raise HTTPException(status_code=500, detail=f"failed to load {noun}: {exc}")

    async def update_entries(request: Request) -> Dict[str, Any]:
        body = await _json_body(request)
        entries = body.get("entries")
        if not isinstance(entries, list):
            raise HTTPException(status_code=400, detail="'entries' must be a list")
        try:
            return _enabled_list_payload(set_fn(entries))
        except Exception as exc:  # noqa: BLE001
            logger.warning("⚠️  Failed to save %s: %s", log_phrase, exc)
            raise HTTPException(status_code=500, detail=f"failed to save {noun}: {exc}")

    get_entries.__name__ = f"get_{handler_slug}"
    update_entries.__name__ = f"update_{handler_slug}"
    if doc:
        get_entries.__doc__ = doc

    router.get(path)(get_entries)
    router.put(path)(update_entries)
    return router


def make_bool_prefs_router(
    load_fn: Callable[[], Any],
    save_fn: Callable[[Any], Any],
    prefs_cls: Type[Any],
    *,
    path: str,
    noun: str = "notify prefs",
    log_noun: Optional[str] = None,
    slug: Optional[str] = None,
    get_doc: Optional[str] = None,
) -> APIRouter:
    """Build a GET/PUT router for one frozen all-boolean preferences dataclass.

    Collapses the shape shared by the alarm notify-prefs and UPS power
    notify-prefs routers (issue #664): a GET that loads the prefs and wraps
    them in ``{prefs, telegram_configured}`` (500 + warning on failure), and a
    PUT that merges whatever boolean fields the body carries over the
    currently-saved values, persists the reconstructed dataclass, and echoes
    the same payload back. Partial bodies are the contract — the UI PUTs one
    toggle at a time — so any field the body omits keeps its saved value.

    The ``src`` half of this pair was deduped into ``src/_toggle_prefs.py``
    long before the router half; this is the router half. ``noun`` is the
    phrase in the HTTP ``detail`` text, ``log_noun`` overrides it for the
    warning line, ``slug`` names the generated handlers.
    """
    log_phrase = log_noun or noun
    handler_slug = slug or re.sub(r"[^a-z0-9]+", "_", noun.lower()).strip("_")

    router = APIRouter()

    def payload(prefs: Any) -> Dict[str, Any]:
        return {"prefs": asdict(prefs), "telegram_configured": is_notify_configured()}

    async def get_prefs() -> Dict[str, Any]:
        try:
            return payload(load_fn())
        except Exception as exc:  # noqa: BLE001
            logger.warning("⚠️  Failed to load %s: %s", log_phrase, exc)
            raise HTTPException(status_code=500, detail=f"failed to load {noun}: {exc}")

    async def update_prefs(request: Request) -> Dict[str, Any]:
        body = await _json_body(request)
        current = asdict(load_fn())
        updated = {key: bool(body.get(key, current[key])) for key in current}
        try:
            prefs = prefs_cls(**updated)
            save_fn(prefs)
            return payload(prefs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("⚠️  Failed to save %s: %s", log_phrase, exc)
            raise HTTPException(status_code=500, detail=f"failed to save {noun}: {exc}")

    get_prefs.__name__ = f"get_{handler_slug}"
    update_prefs.__name__ = f"update_{handler_slug}"
    if get_doc:
        get_prefs.__doc__ = get_doc

    router.get(path)(get_prefs)
    router.put(path)(update_prefs)
    return router


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
