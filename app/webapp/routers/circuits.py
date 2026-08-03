"""Per-circuit energy API over the Athom CT-clamp meters (issue #25).

``GET /api/circuits`` returns every discovered meter with **all** of its
channels — clamp fitted or not. ``PUT /api/circuits/{key}/display_name`` labels
one channel and ``PUT /api/circuits/{key}/invert`` corrects a clamp that was
fitted backwards, both keyed by ``"<meter_id>:<channel>"``.

Where ``energy.py`` answers *how much* the house is drawing, this answers *where
it is going* — the measurement the solar load-balancing automation needs before
it can shift one specific load to match PV surplus.

Partial data is normal and returned with 200, the same contract as
``/api/energy``: a meter that has dropped off Wi-Fi renders as
``reachable=false`` with its channels still listed and ``null`` readings, while
every other meter stays live. ``null`` is never collapsed into ``0`` — 0 W is a
real answer here (an idle circuit, or a channel with no clamp yet) and has to
stay distinguishable from "not measured".
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.webapp.routers._helpers import make_display_name_endpoint
from src.athom_client import (
    CircuitReading,
    CircuitsState,
    MeterState,
    clear_read_cache,
    fetch_circuits_state,
)
from src.circuit_prefs import (
    load_circuit_display_names,
    load_hidden_channels,
    set_circuit_display_name,
    set_circuit_hidden,
    set_circuit_inverted,
)

logger = logging.getLogger(__name__)

router = APIRouter()

#: A meter id that is a real MAC, as :func:`src.athom_client._normalise_mac`
#: writes it. A statically-configured meter (``ATHOM_METER_HOSTS``) instead gets
#: ``"host:<ip>"`` until it is read, and has no MAC to report.
_MAC_RE = re.compile(r"^[0-9A-F]{2}(?::[0-9A-F]{2}){5}$")


def _mac_or_none(meter_id: str) -> Optional[str]:
    """The meter's MAC, or ``None`` when its id isn't one.

    The id already *is* the normalised MAC for an mDNS-discovered meter, but the
    card should not print ``host:192.0.2.73`` under a "MAC" label — that is a
    different fact wearing the same field.
    """
    return meter_id if _MAC_RE.match(meter_id or "") else None


def _channel_dict(
    reading: CircuitReading, names: Dict[str, str], hidden: Dict[str, bool]
) -> Dict[str, Any]:
    """Flatten one channel, merging in its custom label and hidden flag.

    ``display_name`` and ``hidden`` are merged here rather than in the client so
    the device layer stays UI-free — the same split the Tuya and MELCloud
    routers use. Hiding is presentation only: a hidden channel is still returned
    in full, flagged, and the card decides whether to draw it.
    """
    return {
        "channel": reading.channel,
        "key": reading.key,
        "display_name": names.get(reading.key) or None,
        "power_w": reading.power_w,
        "power_raw_w": reading.power_raw_w,
        "current_a": reading.current_a,
        "energy_kwh": reading.energy_kwh,
        "inverted": reading.inverted,
        "hidden": bool(hidden.get(reading.key)),
    }


def _meter_dict(
    meter: MeterState, names: Dict[str, str], hidden: Dict[str, bool]
) -> Dict[str, Any]:
    """Flatten one meter and all of its channels."""
    return {
        "meter_id": meter.meter_id,
        "name": meter.name,
        # The meter's own MAC, so a renamed meter can still be matched to the
        # box on the DIN rail (and to its row in the Network tab).
        "mac": _mac_or_none(meter.meter_id),
        # A meter is renamed through the same store and the same endpoint as its
        # channels, keyed by the bare meter id — so "Athom Energy Monitor ddee01"
        # can become "cuadro principal" once there is more than one of them.
        "display_name": names.get(meter.meter_id) or None,
        "model": meter.model,
        "host": meter.host,
        "reachable": meter.reachable,
        "error": meter.error,
        "voltage_v": meter.voltage_v,
        "frequency_hz": meter.frequency_hz,
        "temperature_c": meter.temperature_c,
        "wifi_rssi_dbm": meter.wifi_rssi_dbm,
        "total_power_w": meter.total_power_w,
        "total_energy_kwh": meter.total_energy_kwh,
        "channels": [_channel_dict(c, names, hidden) for c in meter.channels],
    }


def _circuits_dict(state: CircuitsState) -> Dict[str, Any]:
    """Flatten a :class:`CircuitsState` into the JSON body the PWA reads."""
    names = load_circuit_display_names()
    hidden = load_hidden_channels()
    meters: List[Dict[str, Any]] = [_meter_dict(m, names, hidden) for m in state.meters]
    return {
        "meters": meters,
        # Kept as its own flag rather than inferred from an empty list: "mDNS
        # could not run" and "mDNS ran and found no meters" are different facts
        # and the card says different things about them.
        "discovery_ok": state.discovery_ok,
        "error": state.error,
    }


@router.get("/api/circuits")
async def get_circuits() -> Dict[str, Any]:
    try:
        state = await fetch_circuits_state()
    except Exception as exc:  # noqa: BLE001 — surface any unexpected error
        logger.warning("⚠️  Failed to read circuits: %s", exc)
        raise HTTPException(status_code=502, detail=f"failed to read circuits: {exc}")
    return _circuits_dict(state)


@router.post("/api/circuits/refresh")
async def refresh_circuits() -> Dict[str, Any]:
    """Explicit UI refresh: re-run mDNS now instead of waiting out the TTL.

    A meter joined in the last few minutes is otherwise invisible until the
    discovery TTL lapses, so the card offers a way to look again now. Only the
    per-meter read cache is dropped (for a fresh live reading); the discovery
    cache is deliberately left alone — wiping it would throw away the previous
    sweep a forced, possibly-partial mDNS browse relies on to avoid dropping a
    meter it simply missed this time (issue #615).
    """
    clear_read_cache()
    try:
        state = await fetch_circuits_state(force=True)
    except Exception as exc:  # noqa: BLE001 — surface any unexpected error
        logger.warning("⚠️  Failed to refresh circuits: %s", exc)
        raise HTTPException(status_code=502, detail=f"failed to refresh circuits: {exc}")
    body = _circuits_dict(state)
    meters = body.get("meters") or []
    live = sum(1 for m in meters if m.get("reachable"))
    if not meters:
        detail = (
            "No Athom energy monitors found — check the meter is powered and on "
            "Wi-Fi, or set ATHOM_METER_HOSTS in .env if mDNS is blocked."
        )
    else:
        detail = f"Found {len(meters)} meter(s); {live} reachable."
    body["refresh"] = {"detail": detail, "found": len(meters), "reachable": live}
    return body


# One channel's label, keyed by "<meter_id>:<channel>" — the same
# strip → setter → 500-on-error shape as the unit / plug / network renames.
make_display_name_endpoint(
    router,
    "/api/circuits/{item_id}/display_name",
    "key",
    set_circuit_display_name,
    log_noun="circuit name",
)


class InvertPayload(BaseModel):
    invert: bool


@router.put("/api/circuits/{key}/invert")
async def update_invert(key: str, payload: InvertPayload) -> Dict[str, Any]:
    """Flip the sign of one channel's power reading.

    The BL0906 reports signed power, so a CT clamp fitted with its arrow against
    the flow reads negative on an ordinary load. Correcting that here means not
    having to reopen a live consumer unit to rotate a clamp.
    """
    try:
        set_circuit_inverted(key, payload.invert)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to save invert flag for %s: %s", key, exc)
        raise HTTPException(status_code=500, detail=f"failed to save invert flag: {exc}")
    # The cached read still holds the old sign, so drop it: the card re-renders
    # from the next read and must not show a stale, uncorrected figure. Only the
    # *read* cache — re-running discovery here can come back empty and blank the
    # card for a flipped clamp, which is exactly what happened in testing.
    clear_read_cache()
    return {"key": key, "invert": payload.invert}


class HiddenPayload(BaseModel):
    hidden: bool


@router.put("/api/circuits/{key}/hidden")
async def update_hidden(key: str, payload: HiddenPayload) -> Dict[str, Any]:
    """Put one channel away in the card, or bring it back (issue #619).

    A 6-channel meter on a 4-breaker board reports two channels that will never
    read anything, and they are noise on a card scanned at a glance. Unlike the
    invert flip this changes nothing about the *reading*, so neither cache is
    dropped — the next poll re-renders with the flag already on the wire.
    """
    try:
        set_circuit_hidden(key, payload.hidden)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to save hidden flag for %s: %s", key, exc)
        raise HTTPException(status_code=500, detail=f"failed to save hidden flag: {exc}")
    return {"key": key, "hidden": payload.hidden}
