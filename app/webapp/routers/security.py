"""RISCO Cloud security API over ``src.risco_client``.

``GET /api/security`` returns the live alarm snapshot, ``GET
/api/security/events`` returns recent events, and the POST endpoints run the
confirmed one-tap actions. The separate native Partial action remains disabled
until its group mapping is known; the backend rejects unsupported actions
instead of guessing.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.webapp.alarm_notify import (
    OUTCOME_ERROR,
    OUTCOME_OK,
    SOURCE_MANUAL,
    record_alarm_action,
)
from src.alarm_notify_prefs import (
    AlarmNotifyPrefs,
    load_alarm_notify_prefs,
    save_alarm_notify_prefs,
)
from src.notify_config import is_notify_configured
from src.presence_engine import note_manual_alarm_action
from src.risco_client import (
    ACTIONS,
    RiscoCommandError,
    RiscoConfigError,
    control_system,
    fetch_events,
    fetch_security_state,
    set_zone_bypass,
)
from src.alarm_scene_config import load_scene_pairings, set_scene_pairings
from src.security_override import load_overrides, set_overrides
from src.security_schedules import load_security_schedules, set_security_schedules
from src.security_display_names import (
    load_security_display_names,
    set_security_display_name,
)
from src.security_hidden import load_hidden_zone_ids, set_zone_hidden
from src.security_trouble_ignore import (
    load_ignored_trouble_zone_ids,
    set_zone_trouble_ignored,
)
from src import telemetry

logger = logging.getLogger(__name__)

router = APIRouter()


def _event_ts(raw: Any) -> Optional[int]:
    """Parse a RISCO event time (UTC ISO-ish) to epoch seconds; None if unparsable."""
    if not raw:
        return None
    try:
        return int(datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def _persist_risco_events(events: List[object]) -> None:
    """Persist RISCO events into the unified telemetry store, deduped (#289).

    The RISCO feed is otherwise ephemeral (live-pulled, aged out by the cloud).
    Dedupe against already-stored events by a ``(time, name, zone)`` signature so
    re-polling — including after a restart — never double-inserts. Best-effort:
    a telemetry failure must never break the live events response. Blocking
    SQLite, so the caller runs this in a worker thread.
    """
    try:
        if not telemetry.default_db_ready():
            return
        existing = telemetry.read_events(domain="security", event_type="panel_event", limit=500)
        seen = {
            e["payload"].get("sig")
            for e in existing
            if isinstance(e.get("payload"), dict)
        }
        for event in events:
            data = asdict(event)
            sig = f"{data.get('time')}|{data.get('name')}|{data.get('zone_id')}"
            if sig in seen:
                continue
            seen.add(sig)
            data["sig"] = sig
            zone_id = data.get("zone_id")
            telemetry.record_event(
                "security",
                "panel_event",
                entity_id=str(zone_id) if zone_id is not None else None,
                source="panel",
                payload=data,
                ts=_event_ts(data.get("time")),
            )
    except Exception:  # noqa: BLE001 — telemetry persistence is best-effort
        logger.debug("telemetry RISCO-event persist skipped", exc_info=True)


def _schedule_payload(entries: List[object]) -> Dict[str, Any]:
    active = [entry for entry in entries if getattr(entry, "enabled", False)]
    return {
        "enabled": bool(active),
        "count": len(active),
        "entries": [asdict(entry) for entry in entries],
    }


def _state_payload(state: object) -> Dict[str, Any]:
    """Serialise a ``SecurityState`` and merge per-detector display-name overrides.

    Detectors carry RISCO names like ``"1"``/``"2"``; the override map (keyed by
    the zone id as a string) is layered on as ``display_name`` per zone, mirroring
    how the units/plugs routers surface custom labels (issue #84).
    """
    payload = asdict(state)  # type: ignore[arg-type]
    overrides = load_security_display_names()
    hidden = load_hidden_zone_ids()
    trouble_ignored = load_ignored_trouble_zone_ids()
    for zone in payload.get("zones") or []:
        zone_id = str(zone.get("id"))
        zone["display_name"] = overrides.get(zone_id) or None
        # Whether the user has parked this detector out of the default list
        # (issue #104). The UI still renders it when "show hidden" is on.
        zone["hidden"] = zone_id in hidden
        # Whether the user has chosen to ignore this detector's trouble flag
        # (issue #225) — ignored troubles render muted and don't bubble to the
        # main card.
        zone["trouble_ignored"] = zone_id in trouble_ignored
    return payload


def _events_payload(events: List[object]) -> Dict[str, Any]:
    return {"events": [asdict(event) for event in events]}


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, RiscoConfigError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, RiscoCommandError):
        return HTTPException(status_code=502, detail=str(exc))
    logger.warning("Failed to call RISCO Cloud: %s", exc)
    return HTTPException(status_code=502, detail=f"failed to call RISCO Cloud: {exc}")


@router.get("/api/security")
async def get_security() -> Dict[str, Any]:
    try:
        payload = _state_payload(await fetch_security_state())
    except (RiscoConfigError, RiscoCommandError) as exc:
        raise _http_error(exc)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)
    return payload


@router.get("/api/security/events")
async def get_security_events(count: int = 50) -> Dict[str, Any]:
    safe_count = max(1, min(int(count or 50), 100))
    try:
        events = await fetch_events(count=safe_count)
        await asyncio.to_thread(_persist_risco_events, events)
        return _events_payload(events)
    except (RiscoConfigError, RiscoCommandError) as exc:
        raise _http_error(exc)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


@router.get("/api/security/schedules")
async def get_security_schedules() -> Dict[str, Any]:
    try:
        return _schedule_payload(load_security_schedules())
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to load alarm schedules: %s", exc)
        raise HTTPException(status_code=500, detail=f"failed to load schedules: {exc}")


@router.put("/api/security/schedules")
async def update_security_schedules(request: Request) -> Dict[str, Any]:
    body = await _json_body(request)
    entries = body.get("entries")
    if not isinstance(entries, list):
        raise HTTPException(status_code=400, detail="'entries' must be a list")
    try:
        return _schedule_payload(set_security_schedules(entries))
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to save alarm schedules: %s", exc)
        raise HTTPException(status_code=500, detail=f"failed to save schedules: {exc}")


def _pairings_payload(pairings: List[object]) -> Dict[str, Any]:
    active = [p for p in pairings if getattr(p, "enabled", False)]
    return {
        "enabled": bool(active),
        "count": len(active),
        "entries": [asdict(p) for p in pairings],
    }


@router.get("/api/security/scene-pairings")
async def get_scene_pairings() -> Dict[str, Any]:
    """Return the detector→camera+preset alarm-scene capture pairings (issue #162)."""
    try:
        return _pairings_payload(load_scene_pairings())
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to load scene pairings: %s", exc)
        raise HTTPException(status_code=500, detail=f"failed to load scene pairings: {exc}")


@router.put("/api/security/scene-pairings")
async def update_scene_pairings(request: Request) -> Dict[str, Any]:
    body = await _json_body(request)
    entries = body.get("entries")
    if not isinstance(entries, list):
        raise HTTPException(status_code=400, detail="'entries' must be a list")
    try:
        return _pairings_payload(set_scene_pairings(entries))
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to save scene pairings: %s", exc)
        raise HTTPException(status_code=500, detail=f"failed to save scene pairings: {exc}")


def _overrides_payload(entries: List[object]) -> Dict[str, Any]:
    active = [e for e in entries if getattr(e, "enabled", False)]
    return {
        "enabled": bool(active),
        "count": len(active),
        "entries": [asdict(e) for e in entries],
    }


@router.get("/api/security/overrides")
async def get_overrides() -> Dict[str, Any]:
    """Return the "auto-bypass after N repeats this session" detector rules (issue #341)."""
    try:
        return _overrides_payload(load_overrides())
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to load alarm overrides: %s", exc)
        raise HTTPException(status_code=500, detail=f"failed to load overrides: {exc}")


@router.put("/api/security/overrides")
async def update_overrides(request: Request) -> Dict[str, Any]:
    body = await _json_body(request)
    entries = body.get("entries")
    if not isinstance(entries, list):
        raise HTTPException(status_code=400, detail="'entries' must be a list")
    try:
        return _overrides_payload(set_overrides(entries))
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to save alarm overrides: %s", exc)
        raise HTTPException(status_code=500, detail=f"failed to save overrides: {exc}")


def _notify_prefs_payload(prefs: AlarmNotifyPrefs) -> Dict[str, Any]:
    return {
        "prefs": asdict(prefs),
        "telegram_configured": is_notify_configured(),
    }


@router.get("/api/security/notify-prefs")
async def get_notify_prefs() -> Dict[str, Any]:
    """Return the automatic-alarm notification toggles + whether Telegram is set up."""
    try:
        return _notify_prefs_payload(load_alarm_notify_prefs())
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to load notify prefs: %s", exc)
        raise HTTPException(status_code=500, detail=f"failed to load notify prefs: {exc}")


@router.put("/api/security/notify-prefs")
async def update_notify_prefs(request: Request) -> Dict[str, Any]:
    body = await _json_body(request)
    current = asdict(load_alarm_notify_prefs())
    updated = {key: bool(body.get(key, current[key])) for key in current}
    try:
        prefs = AlarmNotifyPrefs(**updated)
        save_alarm_notify_prefs(prefs)
        return _notify_prefs_payload(prefs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to save notify prefs: %s", exc)
        raise HTTPException(status_code=500, detail=f"failed to save notify prefs: {exc}")


@router.post("/api/security/{action}")
async def post_security_action(action: str) -> Dict[str, Any]:
    if action not in ACTIONS:
        raise HTTPException(status_code=400, detail=f"unknown action '{action}'")
    try:
        state = await control_system(action)
        note_manual_alarm_action(action)
        # Manual actions are logged for the local activity record but never push
        # a notification — the user is already at the app.
        record_alarm_action(source=SOURCE_MANUAL, action=action, outcome=OUTCOME_OK)
        return _state_payload(state)
    except (RiscoConfigError, RiscoCommandError) as exc:
        record_alarm_action(
            source=SOURCE_MANUAL, action=action, outcome=OUTCOME_ERROR, error=str(exc)
        )
        raise _http_error(exc)
    except Exception as exc:  # noqa: BLE001
        record_alarm_action(
            source=SOURCE_MANUAL, action=action, outcome=OUTCOME_ERROR, error=str(exc)
        )
        raise _http_error(exc)


@router.post("/api/security/zones/{zone_id}/bypass")
async def post_zone_bypass(zone_id: int, request: Request) -> Dict[str, Any]:
    bypass = await _bool_field(request, "bypass")
    try:
        return _state_payload(await set_zone_bypass(zone_id, bypass))
    except (RiscoConfigError, RiscoCommandError) as exc:
        raise _http_error(exc)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


class DisplayNamePayload(BaseModel):
    display_name: str


@router.put("/api/security/zones/{zone_id}/display_name")
async def update_zone_display_name(
    zone_id: int, payload: DisplayNamePayload
) -> Dict[str, Any]:
    name = payload.display_name.strip()
    try:
        set_security_display_name(str(zone_id), name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to save detector name for %s: %s", zone_id, exc)
        raise HTTPException(status_code=500, detail=f"failed to save display name: {exc}")
    return {"zone_id": zone_id, "display_name": name or None}


class HiddenPayload(BaseModel):
    hidden: bool


@router.put("/api/security/zones/{zone_id}/hidden")
async def update_zone_hidden(
    zone_id: int, payload: HiddenPayload
) -> Dict[str, Any]:
    try:
        set_zone_hidden(str(zone_id), payload.hidden)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to save detector hidden flag for %s: %s", zone_id, exc)
        raise HTTPException(status_code=500, detail=f"failed to save hidden flag: {exc}")
    return {"zone_id": zone_id, "hidden": payload.hidden}


class TroubleIgnoredPayload(BaseModel):
    ignored: bool


@router.put("/api/security/zones/{zone_id}/trouble_ignored")
async def update_zone_trouble_ignored(
    zone_id: int, payload: TroubleIgnoredPayload
) -> Dict[str, Any]:
    try:
        set_zone_trouble_ignored(str(zone_id), payload.ignored)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to save detector trouble-ignore for %s: %s", zone_id, exc)
        raise HTTPException(status_code=500, detail=f"failed to save trouble-ignore: {exc}")
    return {"zone_id": zone_id, "trouble_ignored": payload.ignored}


async def _json_body(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="expected a JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    return body


async def _bool_field(request: Request, name: str) -> bool:
    body = await _json_body(request)
    value = body.get(name)
    if not isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"'{name}' must be a boolean")
    return value
