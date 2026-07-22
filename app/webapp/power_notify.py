"""Record + (conditionally) notify on UPS mains-power transitions.

Mirrors :mod:`app.webapp.alarm_notify` for the power domain. Two entry points
used by the background power monitor:

- :func:`record_power_event` — the UPS crossed between mains and battery.
- :func:`record_low_battery_shutdown` / :func:`record_low_battery_shutdown_cancelled`
  — the safety-net pair for the low-battery auto-shutdown: fired when the UPS
  runtime drops critically low while on battery, and when mains power returns
  before the scheduled shutdown completes.

All of them share the same three-step shape:

1. **Always** appends the event to the local activity log (``logs/power.jsonl``).
2. **Conditionally** sends a Telegram message — only when the matching
   :class:`PowerNotifyPrefs` toggle is on and a notifier is configured, via
   :func:`_send_with_retry` (one short retry on a transient failure — see its
   docstring). A delivery failure that survives the retry is logged and
   swallowed so it can never break the monitor.
3. For the shutdown pair, the OS-level action (:mod:`src.host_shutdown`) is
   invoked through an injectable seam so tests never touch the real ``shutdown``
   command.

Edge-triggering (fire only on a transition/threshold-crossing, with a baseline
on first observation) is the caller's job — see :mod:`app.webapp.power_monitor`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx

from src.activity_log import append_activity
from src.host_shutdown import cancel_shutdown, initiate_shutdown
from src.notify import Notifier, NotifierError
from src.notify_config import build_alarm_notifier
from src.pc_fleet_prefs import PcFleetPrefs, load_pc_fleet_prefs
from src.power_notify_prefs import PowerNotifyPrefs, load_power_notify_prefs

logger = logging.getLogger(__name__)

# Hub loopback API (machine inventory + per-machine shutdown). Loopback needs
# no auth token. Base URL overridable via env for tests / non-default ports.
_HUB_BASE_ENV = "PC_FLEET_HUB_BASE"
_DEFAULT_HUB_BASE = "http://127.0.0.1:8000"
_HUB_TIMEOUT_S = 5.0
# Best-effort confirmation: one short wait then a single re-poll. The whole
# confirmation step is capped (wait + one ≤5s re-poll ≈ ≤9s) so it can never
# hold the tower's own shutdown past the budget — the UPS clock is ticking.
_CONFIRM_WAIT_S = 4.0
# A machine is only shut down when the hub reports it live; every other state
# (down / dormant / probe-error) is a NORMAL "already down, skipped" outcome —
# a satellite on raw grid power may already be gone when this fires.
_ALIVE_STATE = "up"

# One short retry before giving up on a Telegram send. These power alerts are
# edge-triggered and fired exactly once per transition (see power_monitor.py)
# — with no retry, a single transient failure (e.g. the router itself
# rebooting for a moment right as mains power returns) permanently loses that
# one alert (issue #394). Kept short since the send is synchronous/blocking.
# Read fresh on every call (not a default-arg binding) so tests can
# monkeypatch it directly.
_SEND_RETRY_DELAYS_S: Tuple[float, ...] = (3.0,)


def _send_with_retry(notifier: Notifier, text: str) -> None:
    """Send ``text`` via ``notifier``, retrying per :data:`_SEND_RETRY_DELAYS_S`.

    Raises the last :class:`NotifierError` only once every retry is
    exhausted — the caller decides whether to log/swallow it, same as a bare
    ``notifier.send_text`` call.
    """

    last_exc: Optional[NotifierError] = None
    for attempt in range(len(_SEND_RETRY_DELAYS_S) + 1):
        try:
            notifier.send_text(text)
            return
        except NotifierError as exc:
            last_exc = exc
            if attempt < len(_SEND_RETRY_DELAYS_S):
                time.sleep(_SEND_RETRY_DELAYS_S[attempt])
    assert last_exc is not None
    raise last_exc


def _compose_message(lost: bool, detail: Optional[str]) -> str:
    suffix = f" · {detail}" if detail else ""
    if lost:
        return f"⚠️ Mains power LOST — PC & Wi-Fi on UPS battery{suffix}"
    return f"✅ Mains power restored — UPS back on mains{suffix}"


async def record_power_event(
    *,
    lost: bool,
    detail: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
    prefs_loader: Callable[[], PowerNotifyPrefs] = load_power_notify_prefs,
    notifier_factory: Callable[[], Optional[Notifier]] = build_alarm_notifier,
) -> None:
    """Log a UPS power transition and notify when its toggle is on.

    Args:
        lost: ``True`` when mains was lost (UPS on battery); ``False`` on restore.
        detail: short human context (e.g. battery runtime) for the message.
        prefs_loader / notifier_factory: injection seams for tests.
    """

    event = "power_lost" if lost else "power_restored"
    record: Dict[str, Any] = {"event": event, "mains_online": not lost}
    if detail:
        record["detail"] = detail
    if extra:
        record.update(extra)
    append_activity("power", record)

    prefs = prefs_loader()
    if not getattr(prefs, event, False):
        return

    notifier = notifier_factory()
    if notifier is None:
        return
    try:
        # _send_with_retry is blocking (network I/O + a sleep-based retry) and
        # this coroutine runs on an async tick sharing uvicorn's single event
        # loop — thread it off so a slow/failing send can't stall the webapp.
        await asyncio.to_thread(_send_with_retry, notifier, _compose_message(lost, detail))
    except NotifierError as exc:  # delivery must never break the monitor loop
        logger.warning("⚠️ Telegram power notification failed: %s", exc)


# ---------------------------------------------------- fleet orchestration


@dataclass
class MachineOutcome:
    """One satellite's result in the low-battery shutdown sweep."""

    machine_id: str
    name: str
    outcome: str  # "shutdown sent" | "excluded" | "already down, skipped" | "failed: …" | "… confirmed down"


@dataclass
class FleetShutdownResult:
    """The satellite sweep: whether the hub answered, and per-machine outcomes."""

    hub_reachable: bool
    outcomes: List[MachineOutcome] = field(default_factory=list)


def _hub_base() -> str:
    return (os.getenv(_HUB_BASE_ENV) or _DEFAULT_HUB_BASE).rstrip("/")


async def _fetch_machines(client: httpx.AsyncClient, base: str) -> Optional[List[Dict[str, Any]]]:
    """GET the hub machine list; ``None`` on any transport/shape failure."""
    try:
        resp = await client.get(f"{base}/admin/api/machines/status")
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("⚠️ Fleet shutdown: hub status fetch failed (%s)", exc)
        return None
    machines = data.get("machines") if isinstance(data, dict) else None
    return machines if isinstance(machines, list) else None


def _detail_of(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("detail"):
            return str(body["detail"])
    except ValueError:
        pass
    return (resp.text or f"HTTP {resp.status_code}").strip()[:160]


async def _post_shutdown(client: httpx.AsyncClient, base: str, machine_id: str) -> str:
    """POST a satellite shutdown; ``"shutdown sent"`` on 2xx else ``"failed: …"``."""
    try:
        resp = await client.post(f"{base}/admin/api/machines/{machine_id}/shutdown")
    except (httpx.HTTPError, OSError) as exc:
        return f"failed: {exc}"
    if resp.is_success:
        return "shutdown sent"
    return f"failed: {_detail_of(resp)}"


async def _confirm_down(
    client: httpx.AsyncClient,
    base: str,
    outcomes: List[MachineOutcome],
    pending_ids: List[str],
    wait_s: float,
) -> None:
    """Best-effort: after a short wait, re-poll once and annotate any machine
    now reported not-up as confirmed. Never raises — confirmation is a nicety,
    not a gate on the tower shutdown."""
    if not pending_ids or wait_s <= 0:
        return
    await asyncio.sleep(wait_s)
    machines = await _fetch_machines(client, base)
    if machines is None:
        return
    state_by_id = {m.get("id"): str(m.get("state") or "").lower() for m in machines}
    by_id = {o.machine_id: o for o in outcomes}
    for mid in pending_ids:
        outcome = by_id.get(mid)
        state = state_by_id.get(mid)
        if outcome is not None and state is not None and state != _ALIVE_STATE:
            outcome.outcome = "shutdown sent, confirmed down"


async def _shutdown_fleet_satellites(
    *,
    excluded: Tuple[str, ...],
    hub_base: Optional[str] = None,
    timeout_s: float = _HUB_TIMEOUT_S,
    confirm_wait_s: float = _CONFIRM_WAIT_S,
) -> FleetShutdownResult:
    """Shut down every included, currently-up satellite via the hub, in order.

    Skips the hub's own host (``is_host`` — the tower goes last via the local
    path), skips ``excluded`` ids, and skips machines the hub reports not-up
    (a normal outcome). Hub unreachable/timeout → ``hub_reachable=False`` and
    an empty sweep, so the caller degrades to a tower-only shutdown.
    """
    base = (hub_base or _hub_base()).rstrip("/")
    excluded_set = set(excluded)
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            machines = await _fetch_machines(client, base)
            if machines is None:
                return FleetShutdownResult(hub_reachable=False)

            outcomes: List[MachineOutcome] = []
            pending: List[str] = []
            for machine in machines:
                mid = machine.get("id")
                if not mid:
                    continue
                name = machine.get("display_name") or mid
                if machine.get("is_host"):
                    continue  # the tower shuts down last, via the local path
                if mid in excluded_set:
                    outcomes.append(MachineOutcome(mid, name, "excluded"))
                    continue
                if str(machine.get("state") or "").lower() != _ALIVE_STATE:
                    outcomes.append(MachineOutcome(mid, name, "already down, skipped"))
                    continue
                result = await _post_shutdown(client, base, mid)
                outcomes.append(MachineOutcome(mid, name, result))
                if result == "shutdown sent":
                    pending.append(mid)

            await _confirm_down(client, base, outcomes, pending, confirm_wait_s)
            return FleetShutdownResult(hub_reachable=True, outcomes=outcomes)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("⚠️ Fleet shutdown: hub unreachable (%s)", exc)
        return FleetShutdownResult(hub_reachable=False)


def _compose_shutdown_message(detail: Optional[str], fleet: FleetShutdownResult) -> str:
    suffix = f" · {detail}" if detail else ""
    lines = [f"🔴 UPS battery critically low — shutting the PC fleet down{suffix}"]
    if not fleet.hub_reachable:
        lines.append("⚠️ Hub unreachable — tower-only shutdown (satellites not reached)")
    elif fleet.outcomes:
        lines += [f"• {o.name}: {o.outcome}" for o in fleet.outcomes]
    else:
        lines.append("• No satellites enrolled")
    lines.append("Tower shutting down last.")
    return "\n".join(lines)


async def record_low_battery_shutdown(
    *,
    detail: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
    grace_seconds: int = 180,
    pc_fleet_loader: Callable[[], PcFleetPrefs] = load_pc_fleet_prefs,
    notifier_factory: Callable[[], Optional[Notifier]] = build_alarm_notifier,
    shutdown_fn: Callable[..., bool] = initiate_shutdown,
    fleet_shutdown_fn: Callable[..., Awaitable[FleetShutdownResult]] = _shutdown_fleet_satellites,
) -> bool:
    """Log a critically-low UPS battery and, if enabled, orchestrate shutdown.

    Gated on the pc-fleet master switch (:attr:`PcFleetPrefs.enabled`, which
    superseded the old ``auto_shutdown_low_battery`` toggle). Off means the
    event is still logged (like every other UPS power event) but *nothing*
    else happens — no Telegram, and no shutdown of any machine, tower
    included ("stay up until the end").

    When on: every included, currently-up satellite is shut down first via the
    hub's loopback API (best-effort, per-machine outcomes recorded — the
    tower's own shutdown is never blocked on a satellite failure or an
    unreachable hub), the Telegram alert reports the per-machine outcomes, and
    only then does the tower's local ``shutdown_fn`` fire (it goes last).
    Returns ``True`` only when the tower shutdown was actually scheduled.
    """

    record: Dict[str, Any] = {"event": "low_battery_shutdown"}
    if detail:
        record["detail"] = detail
    if extra:
        record.update(extra)

    prefs = pc_fleet_loader()
    if not prefs.enabled:
        # Master off: log-only, no Telegram, no shutdown of any machine.
        record["fleet_enabled"] = False
        append_activity("power", record)
        return False

    # Satellites first, over the hub — never let this block the tower.
    fleet = await fleet_shutdown_fn(excluded=prefs.excluded)
    record["fleet_enabled"] = True
    record["hub_reachable"] = fleet.hub_reachable
    record["fleet"] = [
        {"id": o.machine_id, "name": o.name, "outcome": o.outcome} for o in fleet.outcomes
    ]
    append_activity("power", record)

    notifier = notifier_factory()
    if notifier is not None:
        try:
            # See record_power_event: blocking send + retry, threaded off the
            # event loop so it can't stall the webapp.
            await asyncio.to_thread(
                _send_with_retry, notifier, _compose_shutdown_message(detail, fleet)
            )
        except NotifierError as exc:  # delivery must never break the monitor loop
            logger.warning("⚠️ Telegram low-battery shutdown notification failed: %s", exc)

    # The tower goes last — after the satellites have been told to power off.
    return shutdown_fn(
        grace_seconds=grace_seconds,
        message="Low UPS battery — PC shutting down to avoid data loss",
    )


def record_low_battery_shutdown_cancelled(
    *,
    cancel_fn: Callable[[], bool] = cancel_shutdown,
) -> None:
    """Log + cancel a previously-scheduled low-battery shutdown.

    Called when mains power returns before the scheduled OS shutdown
    completes. Cancelling is unconditional (not gated on the master switch) —
    it is always safe/idempotent to cancel a shutdown that may or may not
    actually be pending. Note this only cancels the **tower's** local
    shutdown: the satellites already received their fire-and-forget SSH
    shutdown from :func:`record_low_battery_shutdown` and cannot be recalled.
    """

    append_activity("power", {"event": "low_battery_shutdown_cancelled"})
    cancel_fn()
