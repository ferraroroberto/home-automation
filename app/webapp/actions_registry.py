"""``action_id`` -> existing device-control call, for the generic actions alias endpoint.

Issue #641: a single stable ``POST /api/actions/{action_id}`` surface so an
external trigger (a Stream Deck button today, anything else later) doesn't
need to know each device family's own path/id shape. Deliberately thin —
every handler here is one call into an already-existing function
(``set_switch``, ``control_system``, ``HomeAssistantClient.call_service``),
never new device logic. Adding a new action is "add one registry entry", not
"write a new endpoint".

The alarm handlers replicate ``post_security_action``'s manual-action side
effects (``note_manual_alarm_action`` + ``record_alarm_action``) rather than
just calling ``control_system`` bare, so a Stream Deck arm/disarm has the same
effect on presence automation and the activity log as tapping the Security
tab — not just the same panel state.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, Optional

import aiohttp

from app.webapp.alarm_notify import (
    OUTCOME_ERROR,
    OUTCOME_OK,
    SOURCE_MANUAL,
    record_alarm_action,
)
from src.ha_client import HomeAssistantClient
from src.presence_engine import note_manual_alarm_action
from src.risco_client import ACTIONS as _ALARM_ACTIONS
from src.risco_client import control_system
from src.tuya_client import set_switch

logger = logging.getLogger(__name__)

# Devices resolved with Roberto for issue #641. There is no UI for re-pointing
# an action — change the id/entity here if the bound device changes.
_PLUG_DEVICE_ID = "bfc158aece14a52035diwf"  # "luz despacho"

# HA's own MELCloud integration exposes one climate.* entity per room
# (independent of this repo's src/melcloud_client.py, which is a separate
# integration against the same units). Confirmed reachable + controllable
# during the #641 investigation — but only via ``climate.set_hvac_mode``:
# ``climate.turn_on``/``turn_off`` 500 on this integration (live-tested
# against the real device), so on/off is modelled as a fixed hvac_mode
# target rather than the generic turn_on/turn_off service.
_AC_CLIMATE_ENTITY = "climate.despacho"
_AC_ON_MODE = "cool"  # this button's fixed "on" target; edit here to change it
_AC_OFF_MODE = "off"

Handler = Callable[[str, Optional[aiohttp.ClientSession]], Awaitable[Dict[str, Any]]]


async def _plug_action(device_id: str, on: bool) -> Dict[str, Any]:
    await asyncio.to_thread(set_switch, device_id, on)
    return {"device_id": device_id, "switch_on": on}


def _make_plug_handler(on: bool) -> Handler:
    async def _handler(_actor: str, _session: Optional[aiohttp.ClientSession]) -> Dict[str, Any]:
        return await _plug_action(_PLUG_DEVICE_ID, on)

    return _handler


def _make_alarm_handler(action: str) -> Handler:
    async def _handler(actor: str, _session: Optional[aiohttp.ClientSession]) -> Dict[str, Any]:
        try:
            state = await control_system(action)
        except Exception as exc:  # noqa: BLE001 — re-raised after logging, router maps it
            await record_alarm_action(
                source=SOURCE_MANUAL, action=action, outcome=OUTCOME_ERROR,
                error=str(exc), actor=actor,
            )
            raise
        note_manual_alarm_action(action)
        await record_alarm_action(
            source=SOURCE_MANUAL, action=action, outcome=OUTCOME_OK, actor=actor
        )
        return {"action": action, "mode": state.mode, "label": state.label}

    return _handler


def _make_ac_handler(entity_id: str, hvac_mode: str) -> Handler:
    async def _handler(_actor: str, session: aiohttp.ClientSession) -> Dict[str, Any]:
        await HomeAssistantClient(session).call_service(
            "climate", "set_hvac_mode", entity_id, hvac_mode=hvac_mode
        )
        return {"entity_id": entity_id, "hvac_mode": hvac_mode}

    return _handler


ACTIONS: Dict[str, Handler] = {
    "plug_on": _make_plug_handler(True),
    "plug_off": _make_plug_handler(False),
    "ac_on": _make_ac_handler(_AC_CLIMATE_ENTITY, _AC_ON_MODE),
    "ac_off": _make_ac_handler(_AC_CLIMATE_ENTITY, _AC_OFF_MODE),
    **{f"alarm_{action}": _make_alarm_handler(action) for action in _ALARM_ACTIONS},
}
