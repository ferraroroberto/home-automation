# Generalized action alias (Stream Deck etc.)

## Generalized action alias (Stream Deck etc.)

`POST /api/actions/{action_id}` is a single stable surface over
the existing per-device-family endpoints, for an external trigger (a physical
Stream Deck button, or
anything else later) that just wants "run this named thing" without knowing
each domain's own path/id shape (`device_id`s, per-domain request bodies).
It carries the same bearer-token requirement as every other API route
(loopback exempt, remote needs `Authorization: Bearer <token>` or `?token=`).

```powershell
curl -X POST https://<host>:8447/api/actions/alarm_disarm -H "Authorization: Bearer <token>"
```

Registered actions:

| `action_id` | Effect |
|-------------|--------|
| `alarm_arm` / `alarm_disarm` / `alarm_partial` / `alarm_perimeter` | Same as `POST /api/security/{action}` — wraps `control_system()` with the same manual-action side effects (presence-engine `note_manual_alarm_action`, `alarm.jsonl` activity entry). |
| `plug_on` / `plug_off` | Toggles the Tuya plug bound in `config/quick_actions.json` (`plug_device_id`, gitignored — see `config/quick_actions.sample.json`) via `set_switch()`; there is no UI for re-pointing it, edit that file directly if the bound device changes. |
| `ac_on` / `ac_off` | Turns one room's MELCloud unit on/off via Home Assistant's own `climate.<room>` entity, using `climate.set_hvac_mode` (`hvac_mode: "cool"` / `"off"`) — `climate.turn_on`/`climate.turn_off` return HTTP 500 on this integration, see note below. |

Each call also records one `domain="action"` telemetry event (`GET /api/activity?domain=action`) tagged with the caller's `X-Automation-Source` header (any short token — this endpoint accepts values beyond the security router's fixed `ha`/`voice-pe` set, since it's meant for arbitrary future external triggers; an absent header records as `external`). This is what makes an action-endpoint call distinguishable from the same device being driven through its own webapp-UI endpoint, which never writes this domain.

`config/quick_actions.json` is read once at webapp start, so editing it needs a `tray.bat --restart`. If the file is absent or a key is unset, the webapp logs a warning at boot naming each missing key (never the values), and the affected actions return a 503 until it is fixed.

Unknown `action_id` → 404. Adding a new action is "add one registry entry" in `app/webapp/actions_registry.py` — a handler that wraps one already-existing device call — not a new endpoint.

> **AC control.** This repo has no integration of its own that turns an AC unit on/off — `src/devices/melcloud_client.py` only reads/sets HVAC mode, temperature, fan, and vanes, never power. However Home Assistant already exposes one `climate.*` entity per room via its own independent MELCloud integration (separate from this repo's), reachable via `HomeAssistantClient.call_service()`. `climate.turn_on`/`climate.turn_off` (the generic HA services one would reach for first) return HTTP 500 on this integration (verified against the real device), so `ac_on`/`ac_off` use `climate.set_hvac_mode` instead — a fixed target mode per direction (`cool` for on, `off` for off; see `_AC_ON_MODE` in `app/webapp/actions_registry.py`), not a "restore the last mode" — good enough for a single fixed Stream Deck button, not a full climate control surface. Only one room's unit is wired; the other five rooms — each with their own `climate.<room>` entity — are reachable the same way if a future action needs them — add a registry entry, no new integration required.
