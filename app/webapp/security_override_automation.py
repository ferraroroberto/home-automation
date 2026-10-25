"""Auto-bypass a detector after repeated same-session alarms (issue #341).

RISCO's panel already auto-omits a repeatedly-triggered zone, but only after an
uncontrolled, undocumented number of repeats — confirmed live in issue #325
(a perimeter zone auto-bypassed itself after 5 alarms in one session).
This module makes that behavior configurable per detector (1-3 repeats,
``src/security/security_override.py``) and proactive, so a windy garden or a roaming
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
the only *unconditional* interval reader of RISCO state (the watched
``/api/security`` snapshot also refetches, #759), kept that way to avoid more
pollers tripping the cloud's third-party rate limit, so this rides the same
read rather than starting a competing poll loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Optional

from app.webapp import read_snapshot
from app.webapp._env import _env_bool, _env_int
from app.webapp._event_log_scan import ALARM_EVENT_TYPE, EventLogScanner
from app.webapp._task_loop import spawn_background_task
from app.webapp._zone_lookup import _zone_name_for
from src import telemetry
from src.security.risco_client import SecurityState, fetch_events, set_zone_bypass
from src.security.security_override import load_overrides
from src.security.security_override_session import load_override_session, save_override_session

logger = logging.getLogger(__name__)

# RISCO event-log ``name`` prefixes for an arming action — confirmed live
# against production events: "Full Set - 'USER 1 MASTER',
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
# disk via ``src.security.security_override_session``, precisely so a restart doesn't
# lose track mid-session (same reasoning as the cursor). Claims after
# handling: the handler rewrites the same session file the cursor lives in.
_scanner = EventLogScanner(
    logger=logger,
    read_fail_msg="⚠️ Alarm-override event-log read failed: %s",
    claim_before_handle=False,
)


def _load_cursor() -> Optional[str]:
    return load_override_session().last_event_time


def _save_cursor(event_time: str) -> None:
    session = load_override_session()
    session.last_event_time = event_time
    save_override_session(session)


async def _set_bypass(zone_id: int, bypass: bool) -> SecurityState:
    """``set_zone_bypass``, then mark the ``/api/security`` snapshot dirty (#759).

    Marked even when it raises: the panel may have taken the command.
    """
    try:
        return await set_zone_bypass(zone_id, bypass)
    finally:
        read_snapshot.invalidate("security")


async def _auto_bypass(zone_id: int, max_retries: int, trigger_count: int) -> None:
    try:
        state = await _set_bypass(zone_id, True)
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
    """Un-bypass every zone this automation bypassed and start a fresh session.

    A zone whose un-bypass call fails (e.g. a transient RISCO Cloud error at
    the arm event) stays in ``auto_bypassed_zones`` so the next arm event
    retries it — clearing it unconditionally would forget it forever and
    leave it bypassed through every later armed session (issue #745).
    """

    session = load_override_session()
    still_bypassed = []
    for zone_id in list(session.auto_bypassed_zones):
        try:
            state = await _set_bypass(zone_id, False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("⚠️ Override un-bypass failed for zone %s: %s", zone_id, exc)
            still_bypassed.append(zone_id)
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
    session.auto_bypassed_zones = still_bypassed
    session.session_counts = {}
    save_override_session(session)


async def _handle_event(event: object, overrides_by_zone: Dict[int, object]) -> None:
    name = str(getattr(event, "name", "") or "")
    if getattr(event, "type", None) == ALARM_EVENT_TYPE and getattr(event, "zone_id", None) is not None:
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

    Never raises. The shared :class:`EventLogScanner` guards against
    overlapping runs and re-checks the on-disk cursor immediately before each
    event is handled, claiming it right after, so two concurrently-running
    scans can't both act on the same event (the cross-process fix in issue
    #339).
    """

    await _scanner.guarded(_scan_once)


async def _scan_once() -> None:
    try:
        overrides_by_zone = {e.zone_id: e for e in load_overrides() if e.enabled}
        session = load_override_session()
        if not overrides_by_zone and not session.auto_bypassed_zones:
            # Nothing configured and nothing pending restoration — skip the
            # RISCO Cloud call entirely rather than polling for no reason.
            return

        await _scanner.scan(
            fetch=fetch_events,
            # Every timed event, not only alarms: arm events restore zones.
            wanted=lambda e: bool(getattr(e, "time", None)),
            load_cursor=_load_cursor,
            save_cursor=_save_cursor,
            handle=lambda event: _handle_event(event, overrides_by_zone),
        )
    except Exception as exc:  # noqa: BLE001 — the docstring's "never raises" contract
        # This coroutine is dispatched detached (`asyncio.create_task`) with no
        # handler, so anything escaping here would surface only at GC time, if at
        # all. Keeping the contract explicit matters more because
        # `load_overrides()` / `load_override_session()` raise on an unreadable
        # store rather than degrade to empty. The next scan simply retries.
        logger.warning("⚠️ Security override event scan failed: %s", exc)


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
    if _scanner.due(config.event_scan_interval_s):
        spawn_background_task(_run_event_scan(config), name="security-override-event-scan")
