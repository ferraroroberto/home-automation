# Elgato lights

Elgato lighting is controlled locally from this app without cloud credentials. The implementation uses the devices' direct LAN HTTP API on port `9123` plus Bonjour/mDNS discovery for `_elg._tcp.local.`; `ELGATO_LIGHT_HOSTS` is the fallback when discovery is blocked.

## Feature

The **Lights** group on the **Devices** tab controls Elgato Key Light style devices directly over the local LAN HTTP API. It is cloud-free at runtime: the backend tries Bonjour/mDNS discovery for `_elg._tcp.local.` and also supports an explicit host fallback for networks where discovery is blocked. Each light is a row (name, brightness and warmth, power switch) like its Plugs and Blinds siblings. Tapping it opens the **light sheet**: power, then brightness and warmth sliders with exact numeric entry beside each, applied as they change. The group's footer has state-aware all-on/all-off buttons for reachable lights, and the sheet's **Name and details** row opens a detail modal that saves a custom label in gitignored `config/elgato_display_names.json`. Labels are keyed by reported MAC address when available, so they survive DHCP/IP changes; older host:port labels still load as a fallback until the next save migrates them. The detail modal shows the original Elgato identity, LAN address, MAC metadata when available, firmware, and colour-temperature readback.

### Configuration

Optional config in `.env`:

| Key | Meaning |
|-----|---------|
| `ELGATO_LIGHT_HOSTS` | Optional comma-separated `host[:port]` list. Leave blank to try mDNS discovery only. The default port is `9123`. |

If discovery finds nothing but the Elgato phone app works, set `ELGATO_LIGHT_HOSTS` to the light's LAN IP and restart the webapp.

Do not commit real light hostnames/IPs, room names, or screenshots containing private room names; this repository is public.

### Endpoints

- `GET /api/lights` — list Elgato lights with reachability, display-name override, durable `display_key`, original name, product, firmware, host/port, optional MAC metadata, power, brightness, and color temperature.
- `POST /api/lights/refresh` — re-run mDNS/configured-host discovery and retry each light endpoint. If a refresh fails while previous cards are still rendered, the tab keeps those cards and reports the failure as partial/stale data rather than saying no lights exist.
- `POST /api/lights/{id}` — set `{"on": true|false}`, `{"brightness": 3..100}`, `{"temperature": 143..344}`, or `{"temperature_k": 2900..7000}`; the response is the live read-back.
- `PUT /api/lights/{id}/display_name` — save or clear the local label override.

## Spike result

- mDNS discovery found three Elgato accessories on the home LAN.
- A no-visible-change write/read-back against a Key Light Air succeeded: `off`, brightness `15`, temperature `203` mired (`4926 K`) read back exactly.
- Key Light Air and Key Light MK.2 report color temperature.
- Light Strip Pro reports `temperature=0`, so the app treats it as brightness/power only and does not send `temperature: 0` on writes.

## Recommendation

Keep the minimal direct client in `src/devices/elgato_client.py` rather than adding `python-elgato` for now. The required surface is small (`GET`/`PUT /elgato/lights`, optional `GET /elgato/accessory-info`), the app already depends on `aiohttp`, and direct handling lets the UI distinguish color-temperature-capable lights from brightness-only lights. Revisit a wrapper only if future work adds RGB color, scenes, or broader Elgato accessory support.

## Control Center parity investigation

The next useful slice was mapped before adding more UI. The investigation used three sources: Elgato's support docs, the installed Windows Control Center files, and read-only LAN probes against the devices discoverable at the time.

### Control inventory

Elgato's own docs split the Control Center surface into a few capability families:

- **Key Light-class devices**: Key Light Mini, Key Light Air, Key Light, and Ring Light are Wi-Fi lights with on/off, brightness, and 2900-7000 K color-temperature control. This matches the current app model for Key Light Air and should also cover Ring Light unless a live probe proves a model-specific difference.
- **Key Light Mini**: adds battery/status controls. The public `python-elgato` client models `GET /elgato/battery-info` and battery energy-saving/studio-mode settings under `PUT /elgato/lights/settings`, but the two live Key Light Air devices return `404` for `GET /elgato/battery-info`, as expected.
- **Light Strip**: Control Center / Stream Deck expose color controls in addition to power and brightness. The maintained `python-elgato` wrapper supports hue/saturation writes through the same `PUT /elgato/lights` endpoint, so a direct-client extension can likely stay small.
- **Light Strip Pro**: Control Center exposes scenes/themes and custom JavaScript scenes. The installed Control Center build includes scene templates named Comet, Dynamic, Flow, Fusion, Glitch, Kaleidoscope, Pendulum, Rainbow, Siren, Spectrum, Stargazer, Trailblazer, and Trickle. Template parameters are simple UI primitives such as `color`, `color_list`, `int`, and `int_enum` for speed, direction, duration, transition type, star count, and pulse width. Control Center persists activated scene instances as `LightStripScenePro` XML with scene id, schema id, name, and parameter values.

### LAN API shape verified

Read-only probes against the discoverable Key Light Air devices confirmed:

- `GET /elgato/accessory-info` returns product, firmware/build, display name, hardware board/revision, features, MAC/serial metadata, and Wi-Fi info. Do not expose or commit MAC, serial, SSID, or private display-name values.
- `GET /elgato/lights` returns `{"numberOfLights": 1, "lights": [{"on": 0|1, "brightness": <int>, "temperature": <mired>}]}` for Key Light Air. Temperature mired values map to the 2900-7000 K range used by the UI.
- `PUT /elgato/lights` is the write path for power, brightness, and temperature, with read-back after the write.
- `GET /elgato/lights/settings` returns transition/default settings: `colorChangeDurationMs`, `switchOnDurationMs`, `switchOffDurationMs`, `powerOnBehavior`, `powerOnBrightness`, and `powerOnTemperature`.
- `GET /elgato/battery-info`, `GET /elgato/scenes`, `GET /elgato/lights/scenes`, and `GET /elgato/lightstrip` return `404` on Key Light Air. That means scene and battery UI must be capability-gated, not assumed globally.

The Light Strip Pro was not discoverable during this investigation, so RGB/scene LAN writes remain unverified on the real strip. A Light Strip Pro seen in the spike reported `temperature=0`; that supports treating it as not color-temperature-capable, but it is not enough to ship RGB or scene control without a fresh live probe.

### Wrapper decision

Keep extending the direct client. `python-elgato` is useful as protocol reference, but adopting it would add a dependency while still leaving the app to own discovery, display-name persistence, per-device capability shaping, API serialization, and PWA UX. Its useful deltas for this repo are small and directly portable:

- `GET /elgato/lights/settings`
- optional `GET /elgato/battery-info`
- `PUT /elgato/lights` with `hue` / `saturation` for Light Strip
- optional `POST /elgato/identify` and `POST /elgato/restart` if guarded diagnostics are wanted later
- `PUT /elgato/accessory-info` for vendor display-name writes, which stays out of scope because this app already has local labels

Reconsider adopting the wrapper only if scene upload/activation turns out to require more protocol surface than the simple `/elgato/lights` and `/elgato/lights/settings` calls.

### Not yet built

The parity work these findings point at — hue/saturation for the Light Strip, battery status for the Key Light Mini, the opportunistic `/elgato/lights/settings` + `/elgato/battery-info` probes, and the capability flags that gate them — is tracked in the repo's GitHub issues, not here. This document stays what it is: the spike result, the verified LAN API shape, and the wrapper decision record.

## Verification

Read all discoverable lights:

```powershell
& .\.venv\Scripts\python.exe -m src.list_elgato_lights
```

Write a light and read back the accepted state:

```powershell
& .\.venv\Scripts\python.exe -m src.list_elgato_lights --id <host>:9123 --on off --brightness 15 --temperature 203
& .\.venv\Scripts\python.exe -m src.list_elgato_lights --id 192.168.0.50:9123 --on on --brightness 40 --kelvin 4000
```
