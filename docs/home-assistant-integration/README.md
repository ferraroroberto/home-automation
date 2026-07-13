# Home Assistant integration

This repo exposes the app's existing FastAPI backend as native Home Assistant entities. It is deliberately a thin adapter: Home Assistant calls the same `/api/*` endpoints as the PWA, and all device logic stays in `src/`.

## Why this exists

Voice control should use Home Assistant's deterministic built-in intents over native entities whenever possible. Once the app's devices are HA entities, commands like "turn on the living-room AC", "turn off the plug", or "arm home" can be handled locally by HA with no LLM on the critical path.

## Installed integration

Copy `custom_components/home_automation_app/` into the HA VM's `/config/custom_components/home_automation_app/`, then add this to `/config/configuration.yaml`:

```yaml
home_automation_app:
  base_url: "https://192.168.0.13:8447"
  token: !secret app_api_authorization
  verify_ssl: false
  scan_interval: 30
```

Use `verify_ssl: false` when HA reaches the app by LAN IP but the app serves a Tailscale `.ts.net` certificate. If HA reaches the app by the matching Tailscale hostname, set it to `true` or omit it.

This reuses the existing live-only `app_api_authorization` secret from the deterministic alarm voice bridge. The integration accepts either the raw token or the existing `Bearer <token>` value, so no new HA secret is required.

Do not commit the token, LAN host names, room names, unit IDs, or HA's live `/config` copy.

## Entities exposed

| App source | HA domain | Notes |
|---|---|---|
| `GET /api/units` / `POST /api/units/{id}` | `climate` | Power, mode, target temperature, fan mode, and vane direction (`swing_mode` = vertical, `swing_horizontal_mode` = horizontal — each exposed only on units whose `has_vane_vertical`/`has_vane_horizontal` capability is true). |
| `GET /api/tuya` / `POST /api/tuya/{id}/switch` | `switch` | Switch-capable Tuya devices only. Hidden/offline devices are unavailable. |
| `GET /api/security` / `POST /api/security/{action}` | `alarm_control_panel` | `arm_away` = full arm, `arm_home` = existing RISCO perimeter command, `arm_night` = existing RISCO partial command, `disarm` = existing disarm endpoint. |
| `GET /api/security` zones | `binary_sensor` | One sensor per RISCO zone, with bypass/trouble attributes. |
| `GET /api/energy` | `sensor` | Grid import/export, PV, house consumption, PV surplus, cumulative grid kWh. |

The alarm mapping intentionally preserves the old deterministic voice behavior: the previous custom `rest_command.alarm_perimeter` and `rest_command.alarm_partial` actions become native HA service calls on `alarm_control_panel.home_alarm`.

`control_security()` (`custom_components/home_automation_app/api.py`) sends `X-Automation-Source: ha` on every alarm POST, so `logs/alarm.jsonl`'s `manual` entries tag this integration's commands with `actor: "ha"` — distinct from the webapp PWA (`actor: "webapp"`, the default) and the voice-PE `rest_command`s (`actor: "voice-pe"`, issue #405). Useful when debugging an unexpected arm/disarm: check `logs/alarm.jsonl` for which caller issued it before assuming a person did.

## Voice usage

After HA discovers the entities, Tier-1 built-in intents should cover the compatible surfaces:

- "turn on `<AC name>`" / "turn off `<AC name>`"
- "set `<AC name>` to 23 degrees"
- "set `<AC name>` swing to `<direction>`" (vertical vane, units with `has_vane_vertical`)
- "set `<AC name>` horizontal swing to `<direction>`" (horizontal vane, units with `has_vane_horizontal`)
- "turn on `<plug name>`" / "turn off `<plug name>`"
- "arm home" → RISCO perimeter
- "arm night" → RISCO partial
- "arm away" → RISCO full arm

Disarm remains safety-sensitive. Keep the existing code-gated custom sentence path from `docs/voice-pe-config/` until an equivalent HA-native code-gated flow is explicitly validated.

## Deployment check

From the HA Terminal & SSH add-on:

```sh
mkdir -p /config/custom_components/home_automation_app
# Copy the repo directory contents there by scp or the HA file path you use.
ha core check
ha core restart
```

Then verify in HA:

1. Settings → Devices & services → Entities: filter `home_automation_app`.
2. Developer Tools → States: confirm the climate, switch, alarm, binary_sensor, and sensor entities have values.
3. Developer Tools → Services: call `alarm_control_panel.alarm_arm_home` on the alarm entity and verify the app's Security tab reports perimeter; then disarm through the existing code-gated voice path or the app.
4. Try a safe voice command first, such as turning a non-critical plug on/off, before testing HVAC or alarm commands.

## Maintenance contract

- Keep entity code under `custom_components/home_automation_app/` and shared REST behavior in `api.py`.
- Do not import `src.*` from the HA integration; HA is a remote client of the app API.
- If an endpoint shape changes, update the PWA/API tests and this integration in the same PR.
- If more domains are added, add a platform file that reuses `HomeAutomationApi` rather than a new HTTP client.
