"""DHCP reservation planner + staged reservation manager over ``src.dhcp_plan``.

Split out of ``routers/network.py`` (issue #328) — the DHCP-plan feature is
self-contained (schema in :mod:`src.dhcp_plan`, per-MAC UI overrides in
:mod:`src.dhcp_overrides`) and grew inside the network router only because
that's where the AP device inventory lived. It still reads the live inventory
via ``src.network_client.fetch_network_state`` for classification, but no
longer shares any state with the main ``/api/network`` snapshot endpoint.

The F6600P router caps its static DHCP binding table at 10 reservations
(``DHCP_BIND_MAX``), so this module is built around that constraint: the plan
(``GET /api/network/dhcp-plan``) classifies every device into a category range
and marks it ``reserved`` / ``create`` / ``change``, then three write paths push
changes to the router — always opt-in, confirm-gated in the UI, never on a poll:

- ``POST /api/network/dhcp-plan/apply`` — apply all (or a selected subset of)
  pending create/change rows from the plan.
- ``POST /api/network/dhcp-bindings/delete`` — free a slot by removing one
  existing reservation (by its firmware ``inst_id``).
- ``POST /api/network/dhcp-bindings`` — manually add one reservation for a
  device the rules can't place, or one not in the live inventory at all.
- ``PUT /api/network/dhcp-overrides/{mac}`` — persist a per-MAC category
  override that the planner folds over ``config/dhcp_plan.json``.
- ``POST /api/network/dhcp-reservations/apply`` — the staged reservation
  manager's single combined batch: removals first (freeing slots), then adds
  (cap-aware).

See README "DHCP reservation plan" (#170) and "Managing the ten slots from the
app" (#176) for the full user-facing behavior and the slot-budget rationale.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
from typing import Any, Dict, List, Mapping, Optional, Set

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.dhcp_overrides import load_dhcp_overrides, set_dhcp_override
from src.dhcp_plan import (
    Assignment,
    DhcpPlan,
    binding_name,
    build_plan,
    device_inputs_from_inventory,
    load_dhcp_plan_config,
)
from src.network_client import (
    DHCP_BIND_MAX,
    NetworkCommandError,
    NetworkConfigError,
    apply_dhcp_bindings,
    apply_dhcp_changes,
    delete_dhcp_binding,
    fetch_dhcp_bindings,
    fetch_network_state,
)
from src.network_display_names import load_network_display_names, normalize_mac

logger = logging.getLogger(__name__)

router = APIRouter()


def _assignment_dict(a: Assignment) -> Dict[str, Any]:
    return {
        "mac": a.mac,
        "label": a.label,
        "category": a.category,
        "current_ip": a.current_ip,
        "planned_ip": a.planned_ip,
        "randomized": a.randomized,
        "status": a.status,
    }


def _bindings_map(rows: List[Mapping[str, Any]]) -> Dict[str, str]:
    """``[{mac, ip, …}]`` from the router → ``{normalised-MAC: reserved-IP}``."""
    return {normalize_mac(r["mac"]): r["ip"] for r in rows if r.get("mac") and r.get("ip")}


def _ip_last_octet(ip: Optional[str]) -> int:
    """Last octet of a dotted IP for a stable numeric sort; 999 if unparseable."""
    try:
        return int((ip or "").rsplit(".", 1)[-1])
    except (ValueError, AttributeError):
        return 999


def _existing_binding_dict(
    row: Mapping[str, Any],
    online_macs: Set[str],
    plan_macs: Set[str],
    overrides: Mapping[str, str],
) -> Dict[str, Any]:
    """Serialise one router static-binding row for the "on the router now" list (#176).

    ``online`` = the MAC is in the current inventory; ``in_plan`` = the planner
    saw/placed it. A reservation held by an offline or retired device reads
    ``online=False`` — exactly the rows the user can delete to free a slot. The
    custom display-name override (if any) gives a friendlier label than the stored
    binding name. ``inst_id`` is the firmware path the delete endpoint needs.
    """
    mac_key = normalize_mac(row.get("mac") or "")
    return {
        "name": row.get("name") or None,
        "mac": row.get("mac"),
        "ip": row.get("ip"),
        "inst_id": row.get("inst_id") or None,
        "display_name": overrides.get(mac_key) or None,
        "online": mac_key in online_macs,
        "in_plan": mac_key in plan_macs,
    }


def _dhcp_plan_dict(
    plan: DhcpPlan,
    existing_rows: Optional[List[Mapping[str, Any]]],
    online_macs: Set[str],
) -> Dict[str, Any]:
    """Serialise a :class:`DhcpPlan` (+ the router's live bindings) for the UI.

    ``existing_rows`` is the **actual** static-binding table (``None`` when the read
    failed). Its length is the authoritative slot occupancy — it counts rows for
    devices that aren't in the plan too (e.g. the gateway's own reservation), which
    a status-derived count would miss. The rows are also surfaced as ``existing`` so
    the UI can list (and offer to delete) reservations even for offline devices —
    the user's lever against the fixed 10-slot cap (issue #176 step 1).
    """
    overrides = load_network_display_names()
    statuses = [a.status for c in plan.categories for a in c.assignments]
    pending = [s for s in statuses if s in ("create", "change")]
    # The router's static-binding table is a fixed-size slot pool. A create needs a
    # free slot; a change re-writes a row it already owns (slot-neutral). When the
    # table was read, warn up front if the planned creates can't all fit — so the
    # cap is visible *before* Apply, not a surprise mid-batch.
    warnings = list(plan.warnings)
    bindings_known = existing_rows is not None
    used = len(existing_rows) if bindings_known else 0
    creates = sum(1 for s in statuses if s == "create")
    free = max(0, DHCP_BIND_MAX - used)
    overflow = max(0, creates - free) if bindings_known else 0
    if overflow:
        warnings.append(
            f"The router holds at most {DHCP_BIND_MAX} DHCP reservations and "
            f"{used} are in use — only {free} slot(s) are free, so {overflow} of "
            f"the {creates} new reservation(s) can't be written until you delete "
            "some below (or untick them before applying)."
        )

    plan_macs = {normalize_mac(a.mac) for c in plan.categories for a in c.assignments}
    plan_macs |= {normalize_mac(a.mac) for a in plan.unassigned}
    existing = (
        sorted(
            (
                _existing_binding_dict(r, online_macs, plan_macs, overrides)
                for r in existing_rows
            ),
            key=lambda e: _ip_last_octet(e["ip"]),
        )
        if bindings_known
        else []
    )
    return {
        "categories": [
            {
                "label": c.label,
                "start": c.start,
                "end": c.end,
                "assignments": [_assignment_dict(a) for a in c.assignments],
            }
            for c in plan.categories
        ],
        "unassigned": [_assignment_dict(a) for a in plan.unassigned],
        "warnings": warnings,
        # How many rows the opt-in "Apply plan" action would write to the router.
        "pending_count": len(pending),
        # Firmware slot budget, so the UI can show "K of N free" / a full-table note.
        "capacity": DHCP_BIND_MAX,
        "reservations_used": used if bindings_known else None,
        "slots_free": free if bindings_known else None,
        "bindings_known": bindings_known,
        # The router's current reservations — listed (and deletable) even for
        # offline devices, so a slot held by a retired device is reclaimable (#176).
        "existing": existing,
        # Category labels for the "assign to a group" dropdown on unassigned rows.
        "category_labels": [c.label for c in plan.categories],
    }


async def _compute_dhcp_plan() -> tuple[DhcpPlan, Optional[List[dict]], Set[str]]:
    """Build the reservation plan from the live inventory + the router's bindings.

    Reads the real static-binding table so the plan marks already-reserved rows and
    drives the create/change/reserved status. The binding read is best-effort: a
    failure yields a binding-less plan (every placed row reads as ``create``)
    rather than failing the whole plan. The webapp's per-MAC group overrides
    (:mod:`src.dhcp_overrides`) are folded **over** the committed config so a UI
    choice wins without editing ``config/dhcp_plan.json`` (issue #176 step 3).

    Returns ``(plan, existing_rows, online_macs)``: ``existing_rows`` is the actual
    binding table (``None`` when the read failed) — the authoritative slot occupancy
    and the "on the router now" list; ``online_macs`` is the set of normalised MACs
    in the current inventory, so a reservation's device can be flagged offline.
    Raises ``NetworkConfigError`` / other inventory-read errors for the caller.
    """
    state = await fetch_network_state()
    overrides = load_network_display_names()
    config = load_dhcp_plan_config()
    ui_overrides = load_dhcp_overrides()
    if ui_overrides:
        config = dataclasses.replace(
            config, overrides={**config.overrides, **ui_overrides}
        )
    online_macs = {normalize_mac(d.mac or "") for d in state.devices if d.mac}
    rows: Optional[List[dict]]
    try:
        rows = await fetch_dhcp_bindings()
        bindings = _bindings_map(rows)
    except Exception as exc:  # noqa: BLE001 — binding read is best-effort
        # None (not {}) → "unknown", so the plan shows status "none" and offers 0
        # to apply rather than falsely claiming every row needs creating.
        logger.warning("⚠️  Could not read DHCP bindings (plan status unknown): %s", exc)
        bindings = None
        rows = None
    devices = device_inputs_from_inventory(state.devices, overrides)
    return build_plan(devices, config, bindings), rows, online_macs


@router.get("/api/network/dhcp-plan")
async def get_dhcp_plan() -> Dict[str, Any]:
    """Compute the ordered DHCP reservation plan (issue #170) — **read-only**.

    Reads the live inventory, classifies each device into a category range from
    ``config/dhcp_plan.json``, folds in the router's existing static bindings, and
    returns the per-category MAC→IP assignment with each row's apply status. No
    router writes happen here — applying is the separate, confirm-gated
    ``POST …/dhcp-plan/apply`` (issue #176). Mirrors ``get_network``'s error
    handling: 503 on missing ``NETWORK_*``, 502 on an unexpected read failure.
    """
    try:
        plan, existing_rows, online_macs = await _compute_dhcp_plan()
    except NetworkConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — surface any unexpected error
        logger.warning("⚠️  Failed to read network for DHCP plan: %s", exc)
        raise HTTPException(status_code=502, detail=f"failed to read network: {exc}")
    return _dhcp_plan_dict(plan, existing_rows, online_macs)


class DhcpApplyPayload(BaseModel):
    # Optional MAC allow-list for a *selective* apply (issue #176 step 2). When
    # given, only the create/change rows whose MAC is in this list are written;
    # absent/None keeps the original "all pending" behaviour (CLI/back-compat).
    macs: Optional[List[str]] = None


@router.post("/api/network/dhcp-plan/apply")
async def apply_dhcp_plan(payload: Optional[DhcpApplyPayload] = None) -> Dict[str, Any]:
    """Push the planned reservations to the router (issue #176) — **opt-in write**.

    Recomputes the plan server-side (never trusts client-supplied IPs), then writes
    only the rows that actually need it — ``create`` (no binding yet) and ``change``
    (bound to a different IP) — one at a time, leaving already-reserved rows
    untouched. An optional ``{"macs": [...]}`` body narrows the write to just those
    devices (selective apply, step 2); absent applies every pending row. Returns a
    per-row ``results`` list; a mid-batch failure is recorded, not raised, so one
    rejected row never silently drops the rest. This is a deliberate, confirm-gated
    user action — the UI gates it behind a styled confirm and nothing here ever
    runs on a poll.
    """
    try:
        plan, _existing_rows, _online_macs = await _compute_dhcp_plan()
    except NetworkConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to read network for DHCP apply: %s", exc)
        raise HTTPException(status_code=502, detail=f"failed to read network: {exc}")

    # A None allow-list = apply everything pending; an explicit list filters to it
    # (normalised so the client's casing/separators don't matter). An empty list is
    # treated as "no selection" → nothing to write.
    allow: Optional[Set[str]] = None
    if payload is not None and payload.macs is not None:
        allow = {normalize_mac(m) for m in payload.macs}

    rows = [
        {"name": binding_name(a.label, a.mac), "mac": a.mac, "ip": a.planned_ip}
        for c in plan.categories
        for a in c.assignments
        if a.status in ("create", "change") and a.planned_ip
        and (allow is None or normalize_mac(a.mac) in allow)
    ]
    if not rows:
        return {"results": [], "applied": 0, "failed": 0, "pending_count": 0}

    try:
        results = await apply_dhcp_bindings(rows)
    except NetworkConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except NetworkCommandError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  DHCP apply failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"apply failed: {exc}")

    applied = sum(1 for r in results if r.get("ok"))
    skipped = sum(1 for r in results if r.get("skipped"))
    # "failed" stays the simple not-applied count the client already relies on;
    # "errored" splits out genuine rejects from rows skipped because the router's
    # table is full, and "table_full" lets the UI explain the real reason.
    errored = sum(1 for r in results if not r.get("ok") and not r.get("skipped"))
    table_full = any(
        r.get("skipped") or "table is full" in (r.get("error") or "")
        for r in results
    )
    return {
        "results": results,
        "applied": applied,
        "failed": len(results) - applied,
        "skipped": skipped,
        "errored": errored,
        "table_full": table_full,
        "capacity": DHCP_BIND_MAX,
        "pending_count": len(rows),
    }


# A firmware instance path like ``DEV.V4DP.Sr.Pl1.Bd1`` — letters, digits, dots.
_INST_ID_RE = re.compile(r"^[A-Za-z0-9._]{1,64}$")
# A MAC as six colon/hyphen-separated hex pairs (validated before any write).
_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$")


class DhcpDeletePayload(BaseModel):
    inst_id: str


@router.post("/api/network/dhcp-bindings/delete")
async def delete_dhcp_reservation(payload: DhcpDeletePayload) -> Dict[str, Any]:
    """Delete one static DHCP reservation by its firmware ``inst_id`` (issue #176).

    Frees a slot in the fixed-size binding table so a new reservation can be added
    — the user's lever against the 10-entry cap (delete a row held by an
    offline/retired device, then apply a new one). Confirm-gated in the UI; never
    runs on a poll. 503 on missing ``NETWORK_*``, 502 on a login/reject.
    """
    inst_id = (payload.inst_id or "").strip()
    if not _INST_ID_RE.match(inst_id):
        raise HTTPException(status_code=400, detail="invalid reservation id")
    try:
        await delete_dhcp_binding(inst_id)
    except NetworkConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except NetworkCommandError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  DHCP binding delete failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"delete failed: {exc}")
    return {"ok": True, "inst_id": inst_id}


class DhcpOverridePayload(BaseModel):
    category: str


@router.put("/api/network/dhcp-overrides/{mac}")
async def update_dhcp_override(mac: str, payload: DhcpOverridePayload) -> Dict[str, Any]:
    """Set or clear a device's DHCP category override, persisted (issue #176 step 3).

    Lets the user place an unmatched / generic-hostname device into a category from
    the webapp; the choice survives refreshes (gitignored ``dhcp_overrides.json``)
    and is folded over the committed config so the planner then slots the device an
    IP in that range. An empty ``category`` clears it. No router write happens here
    — applying the resulting plan is the separate, confirm-gated step.
    """
    category = (payload.category or "").strip()
    config = load_dhcp_plan_config()
    if category and category not in set(config.labels):
        raise HTTPException(
            status_code=400,
            detail=f"unknown category '{category}' — must be one of the configured ranges",
        )
    try:
        await asyncio.to_thread(set_dhcp_override, mac, category)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to save DHCP override for %s: %s", mac, exc)
        raise HTTPException(status_code=500, detail=f"failed to save override: {exc}")
    return {"mac": normalize_mac(mac), "category": category or None}


class DhcpManualBindingPayload(BaseModel):
    mac: str
    ip: str
    name: Optional[str] = None


@router.post("/api/network/dhcp-bindings")
async def add_dhcp_reservation(payload: DhcpManualBindingPayload) -> Dict[str, Any]:
    """Manually add one static reservation ``{mac, ip, name?}`` (issue #176 step 3).

    For a device the rules can't place, or one not in the live inventory at all. The
    IP is validated server-side (well-formed MAC, in the configured subnet, a host
    octet, and not already held by a *different* MAC) before the write, which goes
    through the same confirm-gated, cap-aware path as the plan apply — so a full
    table returns a clear "delete some first" rather than a silent failure.
    """
    mac = (payload.mac or "").strip()
    ip = (payload.ip or "").strip()
    if not _MAC_RE.match(mac):
        raise HTTPException(status_code=400, detail="invalid MAC address")
    config = load_dhcp_plan_config()
    prefix = config.subnet_prefix.rstrip(".")
    octet = _ip_last_octet(ip)
    if not ip.startswith(prefix + ".") or not (2 <= octet <= 254):
        raise HTTPException(
            status_code=400,
            detail=f"IP must be on {prefix}.0/24 with a host octet (2–254)",
        )
    # Reject an IP already held by a different MAC (a duplicate would collide); a
    # re-add for the same MAC is an idempotent replace, which the writer handles.
    try:
        existing = await fetch_dhcp_bindings()
        for row in existing:
            if row.get("ip") == ip and normalize_mac(row.get("mac") or "") != normalize_mac(mac):
                raise HTTPException(
                    status_code=409,
                    detail=f"{ip} is already reserved for another device",
                )
    except HTTPException:
        raise
    except NetworkConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — pre-check is best-effort
        logger.warning("⚠️  Could not pre-check existing bindings: %s", exc)

    name = binding_name((payload.name or "").strip() or mac, mac)
    try:
        results = await apply_dhcp_bindings([{"name": name, "mac": mac, "ip": ip}])
    except NetworkConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except NetworkCommandError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Manual DHCP binding failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"add failed: {exc}")
    row = results[0] if results else {"ok": False, "error": "no result"}
    if not row.get("ok"):
        # Surface the real reason (e.g. the full-table cap) with a 409, not a 200.
        raise HTTPException(status_code=409, detail=row.get("error") or "router rejected the reservation")
    return {"ok": True, "mac": normalize_mac(mac), "ip": ip, "name": name}


class DhcpReservationsApplyPayload(BaseModel):
    # A staged batch from the reservation manager (#176 redesign): rows to delete
    # by inst_id, plan rows to add by MAC (IPs recomputed server-side), and manual
    # rows to add. All applied in one router session — deletes first, then adds.
    remove: List[str] = []
    add_macs: List[str] = []
    add_manual: List[DhcpManualBindingPayload] = []


@router.post("/api/network/dhcp-reservations/apply")
async def apply_dhcp_reservations(payload: DhcpReservationsApplyPayload) -> Dict[str, Any]:
    """Apply a staged batch of reservation changes at once (issue #176 redesign).

    The card lets the user mark router rows to **remove**, plan/unassigned rows to
    **add**, and **manual** rows to add, then applies them all together: deletes
    first (freeing slots), then adds (cap-aware). IPs for ``add_macs`` are recomputed
    server-side from the live plan — never trusted from the client. Confirm-gated;
    never on a poll. Returns a combined per-op result.
    """
    try:
        plan, _existing_rows, _online = await _compute_dhcp_plan()
    except NetworkConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to read network for DHCP apply: %s", exc)
        raise HTTPException(status_code=502, detail=f"failed to read network: {exc}")

    # Map each requested plan MAC → its planned {name, mac, ip} (create/change only).
    want = {normalize_mac(m) for m in payload.add_macs}
    add_rows: List[Dict[str, Any]] = [
        {"name": binding_name(a.label, a.mac), "mac": a.mac, "ip": a.planned_ip}
        for c in plan.categories
        for a in c.assignments
        if a.status in ("create", "change") and a.planned_ip and normalize_mac(a.mac) in want
    ]

    # Validate + append the manual rows (same rules as the manual-add endpoint).
    config = load_dhcp_plan_config()
    prefix = config.subnet_prefix.rstrip(".")
    for m in payload.add_manual:
        mac = (m.mac or "").strip()
        ip = (m.ip or "").strip()
        if not _MAC_RE.match(mac):
            raise HTTPException(status_code=400, detail=f"invalid MAC address: {mac}")
        octet = _ip_last_octet(ip)
        if not ip.startswith(prefix + ".") or not (2 <= octet <= 254):
            raise HTTPException(
                status_code=400,
                detail=f"IP must be on {prefix}.0/24 with a host octet (2–254): {ip}",
            )
        add_rows.append(
            {"name": binding_name((m.name or "").strip() or mac, mac), "mac": mac, "ip": ip}
        )

    # Validate the remove inst_ids (firmware paths) before any router call.
    removes: List[str] = []
    for inst_id in payload.remove:
        sid = (inst_id or "").strip()
        if not _INST_ID_RE.match(sid):
            raise HTTPException(status_code=400, detail=f"invalid reservation id: {inst_id}")
        removes.append(sid)

    if not removes and not add_rows:
        return {"results": [], "removed": 0, "added": 0, "failed": 0, "capacity": DHCP_BIND_MAX}

    try:
        results = await apply_dhcp_changes(removes, add_rows)
    except NetworkConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except NetworkCommandError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  DHCP changes apply failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"apply failed: {exc}")

    removed = sum(1 for r in results if r.get("op") == "remove" and r.get("ok"))
    added = sum(1 for r in results if r.get("op") == "add" and r.get("ok"))
    failed = sum(1 for r in results if not r.get("ok"))
    table_full = any(
        r.get("skipped") or "table is full" in (r.get("error") or "") for r in results
    )
    return {
        "results": results,
        "removed": removed,
        "added": added,
        "failed": failed,
        "table_full": table_full,
        "capacity": DHCP_BIND_MAX,
    }
