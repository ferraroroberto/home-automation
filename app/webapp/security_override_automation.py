"""Auto-bypass a detector after repeated same-session alarms (issue #341).

RISCO's panel already auto-omits a repeatedly-triggered zone, but only after an
uncontrolled, undocumented number of repeats — confirmed live in issue #325
(zone 12 "PUERTA JARDIN" auto-bypassed itself after 5 alarms in one session).
This module makes that behavior configurable per detector (1-3 repeats,
``src/security_override.py``) and proactive, so a windy garden or a roaming
animal stops re-triggering the scene-capture/notify pipeline (issue #162) well
before the panel's own limit — while leaving the rest of the system armed.

Reuses the event-log-diff architecture issue #325 introduced in
``alarm_scene_automation.py`` (read ``fetch_events()``, diff against a
persisted cursor) rather than the live ``ongoing_alarm``/``memory_alarm``/
per-zone ``triggered`` flags, which that issue documented as unreliable for
repeated same-session alarms. Unlike the scene automation, this must also
observe *arm* events (not just alarms) so a new armed session can restore any
zone it bypassed — so :func:`consider_security_read` runs every tick, not only
while an alarm is active.

Hooked into ``app/webapp/presence_automation.py``'s tick alongside
``alarm_scene_automation.consider_security_read`` — that loop is documented as
the *only* interval reader of RISCO state specifically to avoid a second
poller tripping the cloud's third-party rate limit, so this rides the same
read rather than starting a competing poll loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional

from app.webapp._env import _env_bool, _env_int
from app.webapp._zone_lookup import _zone_name_for
from src import telemetry
from src.risco_client import fetch_events, set_zone_bypass
from src.security_override import load_overrides
from src.security_override_session import load_override_session, save_override_session

logger = logging.getLogger(__name__)

# pyrisco's event-log ``type`` for a genuine "system has gone off" alarm entry
# (matches ``alarm_scene_automation._ALARM_EVENT_TYPE``, confirmed live #325).
_ALARM_EVENT_TYPE = "triggered"

# RISCO event-log ``name`` prefixes for an arming action — confirmed live
# against production events (see issue #341): "Full Set - 'USER 1 MASTER',
# WEB" / "Perimeter Set - 'USER 1 MASTER', WEB". A fresh armed session starts
# here, so any zone this automation bypassed gets restored on the next one.
_ARM_EVENT_NAME_PREFIXES = ("Full Set", "Perimeter Set")


@dataclass(frozen=True)
class OverrideAutomationConfig:
    """Alarm-override engine knobs read from ``.env``."""

    enabled: bool = True
    # How often to diff the RISCO event log. Bounded above 10s deliberately —
    # same rationale as ``AlarmSceneConfig.event_scan_interval_s``.
    event_scan_interval_s: int = 20


def load_override_automation_config() -> OverrideAutomationConfig:
    """Read alarm-override settings from the already-loaded process env.

    Does not ``load_dotenv`` itself — called every presence tick, and the
    presence engine's own startup load already populated the env.
    """

    return OverrideAutomationConfig(
        enabled=_env_bool("SECURITY_OVERRIDE_ENABLED", True),
        event_scan_interval_s=max(10, _env_int("SECURITY_OVERRIDE_EVENT_SCAN_S", 20)),
    )


# Process-lifetime cadence/overlap guard only — the actual scan progress (the
# cursor + per-zone counts + which zones are auto-bypassed) is persisted to
# disk via ``src.security_override_session``, precisely so a restart doesn't
# lose track mid-session (same reasoning as issue #325's cursor).
_state: Dict[str, object] = {"last_scan": None, "scan_running": False}


async def _auto_bypass(zone_id: int, max_retries: int, trigger_count: int) -> None:
    try:
        state = await set_zone_bypass(zone_id, True)
    except Exception as exc:  # noqa: BLE001 — a detached task must never crash silently
        logger.warning("⚠️ Override auto-bypass failed for zone %s: %s", zone_id, exc)
        return
    zone_name = _zone_name_for(zone_id, state)
    telemetry.record_event(
        "security",
        "auto_bypass",
        entity_id=str(zone_id),
        source="override_automation",
        severity="warning",
        payload={
            "zone_name": zone_name,
            "max_retries": max_retries,
            "trigger_count": trigger_count,
        },
    )
    session = load_override_session()
    if zone_id not in session.auto_bypassed_zones:
        session.auto_bypassed_zones.append(zone_id)
    session.session_counts[str(zone_id)] = 0
    save_override_session(session)
    logger.info(
        "🛡️ Auto-bypassed zone %s (%s) after %d/%d triggers this session",
        zone_id, zone_name, trigger_count, max_retries,
    )


async def _restore_after_rearm() -> None:
    """Un-bypass every zone this automation bypassed and start a fresh session."""

    session = load_override_session()
    for zone_id in list(session.auto_bypassed_zones):
        try:
            state = await set_zone_bypass(zone_id, False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("⚠️ Override un-bypass failed for zone %s: %s", zone_id, exc)
            continue
        zone_name = _zone_name_for(zone_id, state)
        telemetry.record_event(
            "security",
            "auto_unbypass",
            entity_id=str(zone_id),
            source="override_automation",
            payload={"zone_name": zone_name},
        )
        logger.info("🔓 Restored zone %s (%s) for the new armed session", zone_id, zone_name)
    session.auto_bypassed_zones = []
    session.session_counts = {}
    save_override_session(session)


async def _handle_event(event: object, overrides_by_zone: Dict[int, object]) -> None:
    name = str(getattr(event, "name", "") or "")
    if getattr(event, "type", None) == _ALARM_EVENT_TYPE and getattr(event, "zone_id", None) is not None:
        zone_id = int(event.zone_id)
        entry = overrides_by_zone.get(zone_id)
        if entry is None:
            return
        session = load_override_session()
        count = session.session_counts.get(str(zone_id), 0) + 1
        session.session_counts[str(zone_id)] = count
        save_override_session(session)
        if count >= entry.max_retries:
            await _auto_bypass(zone_id, entry.max_retries, count)
    elif name.startswith(_ARM_EVENT_NAME_PREFIXES):
        await _restore_after_rearm()


async def _run_event_scan(config: OverrideAutomationConfig) -> None:
    """Diff RISCO's event log against the persisted cursor; act on each new event.

    Never raises. Guarded against overlapping runs the same way
    ``alarm_scene_automation._run_event_scan`` is; the on-disk cursor is
    re-checked and claimed immediately before each event is handled (not
    after) so two concurrently-running scans can't both act on the same event
    (mirrors the cross-process fix in issue #339).
    """

    if _state.get("scan_running"):
        return
    _state["scan_running"] = True
    try:
        overrides_by_zone = {e.zone_id: e for e in load_overrides() if e.enabled}
        session = load_override_session()
        if not overrides_by_zone and not session.auto_bypassed_zones:
            # Nothing configured and nothing pending restoration — skip the
            # RISCO Cloud call entirely rather than polling for no reason.
            return

        try:
            events = await fetch_events()
        except Exception as exc:  # noqa: BLE001 — a detached task must never crash silently
            logger.warning("⚠️ Alarm-override event-log read failed: %s", exc)
            return

        relevant = sorted(
            (e for e in events if getattr(e, "time", None)),
            key=lambda e: e.time,
        )
        if not relevant:
            return

        cursor = session.last_event_time
        if cursor is None:
            # First run ever — establish the baseline without replaying
            # fetch_events()'s full history window as new events.
            session.last_event_time = relevant[-1].time
            save_override_session(session)
            return

        new_events = [e for e in relevant if e.time > cursor]
        if not new_events:
            return

        for event in new_events:
            latest = load_override_session()
            if latest.last_event_time is not None and latest.last_event_time >= event.time:
                continue
            await _handle_event(event, overrides_by_zone)
            latest = load_override_session()
            latest.last_event_time = event.time
            save_override_session(latest)
    finally:
        _state["scan_running"] = False


def _scan_due(config: OverrideAutomationConfig, *, now: Optional[float] = None) -> bool:
    """True when enough time has elapsed since the last event-log scan."""

    instant = time.monotonic() if now is None else now
    last = _state.get("last_scan")
    if last is not None and (instant - float(last)) < config.event_scan_interval_s:
        return False
    _state["last_scan"] = instant
    return True


def consider_security_read(security: object) -> None:
    """Entry point called from the presence loop with its one RISCO read.

    Unlike ``alarm_scene_automation.consider_security_read``, this runs every
    tick regardless of whether an alarm is currently active — it also needs to
    observe arm events to restore a previously auto-bypassed zone.
    """

    del security  # not needed directly; the event log carries everything
    config = load_override_automation_config()
    if not config.enabled:
        return
    if _scan_due(config):
        asyncio.create_task(_run_event_scan(config), name="security-override-event-scan")
