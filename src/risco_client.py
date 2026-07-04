"""
RISCO Cloud security client
===========================
Async, UI-free core for the Security tab (issue #43): read the alarm state,
arm/disarm the system, bypass detectors, and pull the event log. Wraps
``pyrisco`` (the same library Home Assistant's ``risco`` integration uses) over
its **Cloud** path - username + password + panel PIN, no browser, no local
panel connection.

Mirrors the shape of the other clients here (``melcloud_client``, ``sma_client``,
``tuya_client``): typed errors, frozen dataclasses for the sanitized state, and
plain functions the router/CLI call. Credentials come from ``.env``.

Each call logs in with a short-lived :class:`RiscoCloud`, does its work, and
closes - no session is held across requests. That keeps things simple and
sidesteps RISCO's concurrent-login limit (use a dedicated sub-account for the
app; see ``.env.example``). Because we never ``subscribe_states()``, every
``get_state()`` is a fresh read rather than a cached push.

Caveat worth knowing: RISCO periodically blocks third-party clients (they 403'd
Home Assistant's user-agent in April 2026). When that happens login fails; the
fix is a ``pyrisco`` upgrade. See the README.

The native RISCO WebUI HTML/JSON scraper (site login, arm/disarm command, raw
state-flags read) lives in :mod:`src.risco_webui` — extracted in issue #328 to
keep that brittle, screen-scraped path visually separate from this typed cloud
fetcher. This module imports its error types and the two entry points it needs
(``_webui_arm_disarm`` for writes, ``_webui_state_flags`` for the extra state
flags) and owns the logic that merges them with the pyrisco state.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Iterable, List, Optional, Tuple

import aiohttp
from dotenv import load_dotenv
from pyrisco.cloud.alarm import Alarm
from pyrisco.cloud.risco_cloud import STATE_URL as _CLOUD_STATE_URL
from pyrisco.cloud.risco_cloud import RiscoCloud
from pyrisco.common import (
    GROUP_ID_TO_NAME,
    CannotConnectError,
    OperationError,
    UnauthorizedError,
)

from src.risco_webui import (
    RiscoCommandError,
    RiscoConfigError,
    _load_credentials,
    _webui_arm_disarm,
    _webui_state_flags,
)

logger = logging.getLogger("risco")

# Optional group hints for labeling partially-set states if a panel exposes
# armed group letters. Commands use the native WebUI ELArm ids (src.risco_webui).
_PERIMETER_GROUP_ENV = "RISCO_PERIMETER_GROUP"
_PARTIAL_GROUP_ENV = "RISCO_PARTIAL_GROUP"

# The four one-tap actions the Security tab exposes.
ACTIONS = ("disarm", "arm", "partial", "perimeter")


@dataclass(frozen=True)
class SecurityZone:
    """One detector/zone, sanitized for UI/CLI callers."""

    id: int
    name: str
    type: Optional[int] = None
    triggered: bool = False
    bypassed: bool = False
    # Generic per-zone trouble flag from the cloud payload. The RISCO cloud API
    # does not label *why* a detector is troubled (battery, tamper, comm-fault),
    # so this is surfaced as a single "Trouble" indicator (issue #84). Per-zone
    # battery state is not exposed by the cloud (issue #220).
    trouble: bool = False


@dataclass(frozen=True)
class SecurityPartition:
    """One partition's arm state (most installs have a single partition)."""

    id: int
    armed: bool = False
    partially_armed: bool = False
    disarmed: bool = True
    arming: bool = False
    triggered: bool = False
    # Armed group letters (A-D) - how "Perimeter" is detected on the panel.
    armed_groups: Tuple[str, ...] = ()


@dataclass(frozen=True)
class SecurityEvent:
    """One event-log entry. ``user_id`` is the actor when RISCO supplies it."""

    time: str
    name: Optional[str] = None
    text: Optional[str] = None
    category: Optional[str] = None
    type: Optional[str] = None
    partition_id: Optional[int] = None
    zone_id: Optional[int] = None
    user_id: Optional[object] = None
    priority: Optional[int] = None
    group: Optional[str] = None


@dataclass(frozen=True)
class SecurityState:
    """Flattened alarm snapshot the router/CLI render."""

    reachable: bool
    label: str
    mode: str
    partitions: List[SecurityPartition] = field(default_factory=list)
    zones: List[SecurityZone] = field(default_factory=list)
    perimeter_supported: bool = False
    supported_actions: Tuple[str, ...] = ()
    assumed_control_panel_state: bool = False
    system_status: Optional[int] = None
    system_ready: Optional[bool] = None
    trouble: Optional[bool] = None
    # System-wide AC-power health from the cloud status payload (issue #99) — set
    # when the panel lost mains and is running on backup. (The cloud's aggregate
    # ``batteryLow`` flag was dropped in #227 as an unreliable proxy.)
    ac_lost: Optional[bool] = None
    alarm_pending: Optional[bool] = None
    ongoing_alarm: Optional[bool] = None
    memory_alarm: Optional[bool] = None
    error: Optional[str] = None


# --------------------------------------------------------------- config
# ``_load_credentials`` lives in ``src.risco_webui`` (imported above) since both
# the cloud connect below and the WebUI login share it.
def _perimeter_group() -> Optional[str]:
    """The configured perimeter group letter (A-D), or None if unset/invalid."""
    load_dotenv(override=True)
    group = (os.getenv(_PERIMETER_GROUP_ENV) or "").strip().upper()
    return group if group in GROUP_ID_TO_NAME else None


def _partial_group() -> Optional[str]:
    """The configured partial-arm group letter (A-D), or None if unset/invalid."""
    load_dotenv(override=True)
    group = (os.getenv(_PARTIAL_GROUP_ENV) or "").strip().upper()
    return group if group in GROUP_ID_TO_NAME else None


def _supported_actions() -> Tuple[str, ...]:
    return ACTIONS


# --------------------------------------------------------------- connection
@asynccontextmanager
async def _connect() -> AsyncIterator[RiscoCloud]:
    """Log in to RISCO Cloud, yield the client, and close it on exit.

    ``login()`` with no session makes ``RiscoCloud`` create and own an aiohttp
    session that ``close()`` tears down - so this owns the whole lifecycle.
    """
    username, password, pin = _load_credentials()
    risco = RiscoCloud(username, password, pin)
    try:
        await risco.login()
    except UnauthorizedError as exc:
        raise RiscoCommandError(
            "RISCO Cloud rejected the credentials - check RISCO_USERNAME, "
            "RISCO_PASSWORD and RISCO_PIN."
        ) from exc
    except CannotConnectError as exc:
        raise RiscoCommandError(
            "Could not reach RISCO Cloud. It periodically blocks third-party "
            f"clients - a pyrisco upgrade usually fixes it. ({exc})"
        ) from exc
    try:
        yield risco
    finally:
        try:
            await risco.close()
        except Exception:  # noqa: BLE001 - best-effort teardown
            logger.debug("RISCO close() raised during teardown", exc_info=True)


# --------------------------------------------------------------- mapping
def _iter_items(value: object) -> Iterable[object]:
    """Normalize pyrisco's list/dict payload collections into values."""
    if isinstance(value, dict):
        return value.values()
    if isinstance(value, (list, tuple)):
        return value
    return ()


def _raw_status(alarm: object) -> dict[str, Any]:
    """Return the raw status dict pyrisco wraps, when available."""
    raw = getattr(alarm, "_raw", None)
    return raw if isinstance(raw, dict) else {}


def _raw_or_attr(obj: object, raw_key: str, attr: str, default: object = None) -> object:
    if isinstance(obj, dict):
        return obj.get(raw_key, default)
    return getattr(obj, attr, default)


def _armed_groups(partition: object) -> Tuple[str, ...]:
    """Best-effort extraction of armed group letters from a Partition.

    The exact shape of ``Partition.groups`` is panel-dependent, so normalize a
    list (indexed by group) or a dict into the armed A-D letters defensively.
    """
    if isinstance(partition, dict):
        groups = partition.get("groups")
    else:
        try:
            groups = partition.groups  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return ()
    armed: List[str] = []
    if isinstance(groups, (list, tuple)):
        for idx, value in enumerate(groups):
            if value and idx < len(GROUP_ID_TO_NAME):
                armed.append(GROUP_ID_TO_NAME[idx])
    elif isinstance(groups, dict):
        for key, value in groups.items():
            if value:
                armed.append(str(key).upper())
    return tuple(armed)


def _zone(zone: object) -> SecurityZone:
    return SecurityZone(
        id=int(_raw_or_attr(zone, "zoneID", "id", 0) or 0),
        name=str(_raw_or_attr(zone, "zoneName", "name", "") or "Zone"),
        type=_raw_or_attr(zone, "zoneType", "type", None),
        triggered=bool(
            zone.get("status") == 1 if isinstance(zone, dict) else getattr(zone, "triggered", False)
        ),
        bypassed=bool(
            zone.get("status") == 2 if isinstance(zone, dict) else getattr(zone, "bypassed", False)
        ),
        trouble=bool(_raw_or_attr(zone, "trouble", "trouble", False)),
    )


def _partition(partition: object) -> SecurityPartition:
    return SecurityPartition(
        id=int(_raw_or_attr(partition, "id", "id", 0) or 0),
        armed=bool(
            partition.get("armedState") == 3
            if isinstance(partition, dict)
            else getattr(partition, "armed", False)
        ),
        partially_armed=bool(
            partition.get("armedState") == 2
            if isinstance(partition, dict)
            else getattr(partition, "partially_armed", False)
        ),
        disarmed=bool(
            partition.get("armedState") == 1
            if isinstance(partition, dict)
            else getattr(partition, "disarmed", True)
        ),
        arming=bool(
            (partition.get("exitDelayTO") or 0) > 0
            if isinstance(partition, dict)
            else getattr(partition, "arming", False)
        ),
        triggered=bool(
            partition.get("alarmState") == 1
            if isinstance(partition, dict)
            else getattr(partition, "triggered", False)
        ),
        armed_groups=_armed_groups(partition),
    )


# The RISCO panel stamps event times with a trailing "Z" but the clock is
# fixed CET (UTC+1) and does *not* observe DST — so the value is panel-local,
# not real UTC. Empirically verified: an event read at 17:32 UTC carried
# "17:58:02Z" (impossible as real UTC), and the UI rendered events ~1h in the
# future. We reinterpret the stamp as UTC+1 and re-emit a *true* UTC instant so
# the UI can render it DST-aware in the viewer's timezone (Europe/Madrid → CEST
# in summer, CET in winter) via the browser, with no hardcoded display offset.
_PANEL_TZ = timezone(timedelta(hours=1))


def _normalize_event_time(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return s
    try:
        naive = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return s  # unrecognised format — leave untouched
    return (
        naive.replace(tzinfo=_PANEL_TZ)
        .astimezone(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _event(event: object) -> SecurityEvent:
    type_name = getattr(event, "type_name", None)
    if isinstance(type_name, (list, tuple)):
        type_name = type_name[0] if type_name else None
    return SecurityEvent(
        time=_normalize_event_time(str(getattr(event, "time", "") or "")),
        name=getattr(event, "name", None),
        text=getattr(event, "text", None),
        category=getattr(event, "category_name", None),
        type=type_name,
        partition_id=getattr(event, "partition_id", None),
        zone_id=getattr(event, "zone_id", None),
        user_id=getattr(event, "user_id", None),
        priority=getattr(event, "priority", None),
        group=getattr(event, "group", None),
    )


def _overall_label(partitions: List[SecurityPartition]) -> str:
    """One headline state for the whole system from its partitions."""
    if not partitions:
        return "Unknown"
    if any(p.triggered for p in partitions):
        return "Triggered"
    if any(p.arming for p in partitions):
        return "Arming"
    if any(p.armed for p in partitions):
        return "Armed"
    if any(p.partially_armed for p in partitions):
        return "Partially armed"
    return "Disarmed"


def _webui_arm_state(webui_flags: dict[str, Any]) -> Optional[str]:
    value = webui_flags.get("arm_state")
    if isinstance(value, str) and value:
        state = value.split(":", 1)[0]
        return state if state in ("A", "D", "P") else None
    part_info = webui_flags.get("part_info")
    if isinstance(part_info, dict):
        if str(part_info.get("armedStr") or "").strip().lower() == "yes":
            return "A"
        if str(part_info.get("partarmedStr") or "").strip().lower() == "yes":
            return "P"
        if str(part_info.get("disarmedStr") or "").strip().lower() == "yes":
            return "D"
    return None


def _webui_partial_label(webui_flags: dict[str, Any]) -> str:
    return str(webui_flags.get("part_arm_string") or "").strip().lower()


def _mode_from_webui(webui_flags: dict[str, Any]) -> Optional[str]:
    arm_state = _webui_arm_state(webui_flags)
    if arm_state == "A":
        return "armed"
    if arm_state == "D":
        return "disarmed"
    if arm_state == "P":
        partial_label = _webui_partial_label(webui_flags)
        if "perimeter" in partial_label:
            return "perimeter"
        return "partial"
    return None


def _mode_from_partitions(partitions: List[SecurityPartition]) -> str:
    if not partitions:
        return "unknown"
    if any(p.triggered for p in partitions):
        return "triggered"
    if any(p.arming for p in partitions):
        return "arming"
    if any(p.armed for p in partitions):
        return "armed"
    if any(p.partially_armed for p in partitions):
        groups = {group for partition in partitions for group in partition.armed_groups}
        perimeter_group = _perimeter_group()
        partial_group = _partial_group()
        if perimeter_group and perimeter_group in groups:
            return "perimeter"
        if partial_group and partial_group in groups:
            return "partial"
        return "perimeter"
    return "disarmed"


def _has_ongoing_alarm(status: dict[str, Any], webui_flags: dict[str, Any]) -> bool:
    ongoing = webui_flags.get("ongoing_alarm")
    memory = webui_flags.get("memory_alarm")
    if ongoing is not None:
        return bool(ongoing)
    if memory is True:
        return False
    return bool(status.get("alarmPending"))


def _top_level_partition(
    status: dict[str, Any],
    webui_flags: dict[str, Any],
) -> Optional[SecurityPartition]:
    """Synthesize partition 0 for panels that omit ``status.partitions``."""
    if status.get("partitions") is not None or "systemStatus" not in status:
        return None
    system_status = status.get("systemStatus")
    arm_state = _webui_arm_state(webui_flags)
    return SecurityPartition(
        id=0,
        # Observed on this panel: systemStatus 0 while unset/disarmed.
        armed=arm_state == "A",
        partially_armed=arm_state == "P",
        disarmed=arm_state == "D" or (arm_state is None and system_status == 0),
        triggered=_has_ongoing_alarm(status, webui_flags),
    )


def _label_from_status(
    status: dict[str, Any],
    partitions: List[SecurityPartition],
    webui_flags: dict[str, Any],
) -> str:
    webui_mode = _mode_from_webui(webui_flags)
    if webui_mode == "perimeter":
        return "Perimeter"
    if webui_mode == "partial":
        return "Partial"
    if partitions:
        return _overall_label(partitions)
    system_status = status.get("systemStatus")
    if system_status == 0:
        return "Disarmed"
    if system_status is not None:
        return f"System status {system_status}"
    return "Unknown"


def _mode_from_status(
    status: dict[str, Any],
    partitions: List[SecurityPartition],
    webui_flags: dict[str, Any],
) -> str:
    webui_mode = _mode_from_webui(webui_flags)
    if webui_mode:
        return webui_mode
    if partitions:
        return _mode_from_partitions(partitions)
    if _has_ongoing_alarm(status, webui_flags):
        return "triggered"
    system_status = status.get("systemStatus")
    if system_status == 0:
        return "disarmed"
    return "unknown"


def _state_from_alarm(
    alarm: object,
    webui_flags: Optional[dict[str, Any]] = None,
) -> SecurityState:
    status = _raw_status(alarm)
    webui_flags = webui_flags or {}
    raw_partitions = status.get("partitions")
    raw_zones = status.get("zones")
    if raw_partitions is not None:
        partition_items = _iter_items(raw_partitions)
    else:
        try:
            partition_items = _iter_items(getattr(alarm, "partitions", None))
        except TypeError:
            partition_items = ()
    if raw_zones is not None:
        zone_items = _iter_items(raw_zones)
    else:
        try:
            zone_items = _iter_items(getattr(alarm, "zones", None))
        except TypeError:
            zone_items = ()
    partitions = [_partition(p) for p in partition_items if p is not None]
    if not partitions:
        synthetic = _top_level_partition(status, webui_flags)
        if synthetic is not None:
            partitions = [synthetic]
    zones = [_zone(z) for z in zone_items if z is not None]
    return SecurityState(
        reachable=True,
        label=_label_from_status(status, partitions, webui_flags),
        mode=_mode_from_status(status, partitions, webui_flags),
        partitions=partitions,
        zones=zones,
        perimeter_supported=True,
        supported_actions=_supported_actions(),
        assumed_control_panel_state=bool(getattr(alarm, "assumed_control_panel_state", False)),
        system_status=status.get("systemStatus"),
        system_ready=status.get("systemReady"),
        trouble=status.get("trouble"),
        ac_lost=status.get("acLost"),
        alarm_pending=status.get("alarmPending"),
        ongoing_alarm=webui_flags.get("ongoing_alarm"),
        memory_alarm=webui_flags.get("memory_alarm"),
    )


# --------------------------------------------------------------- reads
# pyrisco's get_state() only falls back to the cloud cache (fromControlPanel=
# False) for the retryable result code 72; a non-retryable "panel momentarily
# unreachable" code such as 26 raises OperationError with no cache attempt. This
# reaches for that same cached snapshot directly, reusing the authenticated
# session, so a transient live-read blip doesn't black out the whole tab.
async def _read_cloud_cached_state(risco: RiscoCloud) -> Alarm:
    """Read the cloud-cached panel snapshot (``fromControlPanel=False``).

    Returns an :class:`Alarm` flagged ``assumed_control_panel_state=True`` so
    callers can tell it is a cached read rather than a fresh live one.
    """
    body = {"fromControlPanel": False, "sessionToken": risco._session_id}
    resp = await risco._authenticated_post(_CLOUD_STATE_URL % risco._site_id, body)
    return Alarm(risco, resp["state"]["status"], True)


async def fetch_security_state() -> SecurityState:
    """Read the alarm snapshot: system state, partitions, and detectors.

    Prefers a fresh live panel read. When that read is momentarily unreachable
    (RISCO returns a non-retryable result code such as 26), fall back to the
    cloud-cached snapshot rather than failing the whole tab - the cache still
    carries the zones and the system battery/trouble flags, and the WebUI flags
    below still supply the authoritative arm state. Mirrors the SMA stale-cloud
    fallback (#94/#95). Only a failure of *both* reads surfaces as an error.
    """
    async with _connect() as risco:
        try:
            alarm = await risco.get_state()
        except OperationError as exc:
            logger.warning(
                "⚠️ RISCO live panel read failed (%s) - falling back to cloud cache",
                exc,
            )
            try:
                alarm = await _read_cloud_cached_state(risco)
            except (
                OperationError,
                UnauthorizedError,
                CannotConnectError,
                KeyError,
                TypeError,
                aiohttp.ClientError,
            ) as cache_exc:
                raise RiscoCommandError(
                    "RISCO panel is temporarily unreachable and its cloud-cached "
                    f"state could not be read either ({cache_exc})."
                ) from cache_exc
    webui_flags: dict[str, Any] = {}
    try:
        webui_flags = await _webui_state_flags()
    except RiscoCommandError:
        logger.info("RISCO WebUI state-flag read failed", exc_info=True)
    state = _state_from_alarm(alarm, webui_flags)
    logger.info(
        "RISCO state: %s (%d partition(s), %d zone(s))%s",
        state.label,
        len(state.partitions),
        len(state.zones),
        " [cached]" if state.assumed_control_panel_state else "",
    )
    return state


async def fetch_events(count: int = 50, days: int = 30) -> List[SecurityEvent]:
    """Read the most recent event-log entries (newest first as RISCO returns)."""
    newer_than = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    async with _connect() as risco:
        raw = await risco.get_events(newer_than, count)
    events = [_event(e) for e in raw or []]
    logger.info("RISCO events: %d entr(y/ies)", len(events))
    return events


# --------------------------------------------------------------- controls
async def _apply(
    risco: RiscoCloud,
    action: str,
    partition_id: int,
    perimeter_group: Optional[str],
    partial_group: Optional[str],
) -> None:
    """Issue one arming action against one partition."""
    if action == "disarm":
        await risco.disarm(partition_id)
    elif action == "arm":
        await risco.arm(partition_id)
    elif action == "partial":
        await risco.group_arm(partition_id, partial_group)
    elif action == "perimeter":
        if perimeter_group:
            await risco.group_arm(partition_id, perimeter_group)
        else:
            # Confirmed by the live probe: native "Perimeter Set" produces
            # eventId 15 ("perimeter/part mode"), which is pyrisco's partial_arm.
            await risco.partial_arm(partition_id)


async def control_system(action: str, partition_id: Optional[int] = None) -> SecurityState:
    """Run an arming action and return the re-read state.

    This panel rejects pyrisco's partition-based control endpoint with "use Arm
    action", so writes intentionally use the same native WebUI whole-panel
    endpoint as the RISCO browser app.
    """
    del partition_id
    if action not in ACTIONS:
        raise RiscoCommandError(f"Unknown action '{action}' (expected one of {ACTIONS}).")
    logger.info("RISCO WebUI %s", action)
    await _webui_arm_disarm(action)
    return await fetch_security_state()


async def set_zone_bypass(zone_id: int, bypass: bool) -> SecurityState:
    """Bypass (omit) or un-bypass one detector, returning the re-read state."""
    async with _connect() as risco:
        try:
            await risco.bypass_zone(zone_id, bypass)
        except OperationError as exc:
            verb = "bypass" if bypass else "un-bypass"
            raise RiscoCommandError(f"RISCO rejected {verb} of zone {zone_id}: {exc}") from exc
        fresh = await risco.get_state()
    logger.info("RISCO zone %s bypass=%s", zone_id, bypass)
    return _state_from_alarm(fresh)
