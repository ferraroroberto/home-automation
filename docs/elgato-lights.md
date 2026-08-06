# Elgato lights

Issue #124 proved that Elgato lighting can be controlled locally from this app without cloud credentials. The implementation uses the devices' direct LAN HTTP API on port `9123` plus Bonjour/mDNS discovery for `_elg._tcp.local.`; `ELGATO_LIGHT_HOSTS` is the fallback when discovery is blocked.

## Spike result

- mDNS discovery found three Elgato accessories on the home LAN.
- A no-visible-change write/read-back against a Key Light Air succeeded: `off`, brightness `15`, temperature `203` mired (`4926 K`) read back exactly.
- Key Light Air and Key Light MK.2 report color temperature.
- Light Strip Pro reports `temperature=0`, so the app treats it as brightness/power only and does not send `temperature: 0` on writes.

## Recommendation

Keep the minimal direct client in `src/elgato_client.py` rather than adding `python-elgato` for now. The required surface is small (`GET`/`PUT /elgato/lights`, optional `GET /elgato/accessory-info`), the app already depends on `aiohttp`, and direct handling lets the UI distinguish color-temperature-capable lights from brightness-only lights. Revisit a wrapper only if future work adds RGB color, scenes, or broader Elgato accessory support.

## Control Center parity investigation

Issue #132 mapped the next useful slice before adding more UI. The investigation used three sources: Elgato's current support docs, the installed Windows Control Center files, and read-only LAN probes against the devices discoverable on 2026-06-24.

### Control inventory

Elgato's own docs split the Control Center surface into a few capability families:

- **Key Light-class devices**: Key Light Mini, Key Light Air, Key Light, and Ring Light are Wi-Fi lights with on/off, brightness, and 2900-7000 K color-temperature control. This matches the current app model for Key Light Air and should also cover Ring Light unless a live probe proves a model-specific difference.
- **Key Light Mini**: adds battery/status controls. The public `python-elgato` client models `GET /elgato/battery-info` and battery energy-saving/studio-mode settings under `PUT /elgato/lights/settings`, but the two live Key Light Air devices returned `404` for `GET /elgato/battery-info`, as expected.
- **Light Strip**: Control Center / Stream Deck expose color controls in addition to power and brightness. The maintained `python-elgato` wrapper supports hue/saturation writes through the same `PUT /elgato/lights` endpoint, so the next direct-client extension can likely stay small.
- **Light Strip Pro**: Control Center exposes scenes/themes and custom JavaScript scenes. The installed Control Center build includes scene templates named Comet, Dynamic, Flow, Fusion, Glitch, Kaleidoscope, Pendulum, Rainbow, Siren, Spectrum, Stargazer, Trailblazer, and Trickle. Template parameters are simple UI primitives such as `color`, `color_list`, `int`, and `int_enum` for speed, direction, duration, transition type, star count, and pulse width. Control Center persists activated scene instances as `LightStripScenePro` XML with scene id, schema id, name, and parameter values.

### LAN API shape verified

Read-only probes against the currently discoverable Key Light Air devices confirmed:

- `GET /elgato/accessory-info` returns product, firmware/build, display name, hardware board/revision, features, MAC/serial metadata, and Wi-Fi info. Do not expose or commit MAC, serial, SSID, or private display-name values.
- `GET /elgato/lights` returns `{"numberOfLights": 1, "lights": [{"on": 0|1, "brightness": <int>, "temperature": <mired>}]}` for Key Light Air. Temperature mired values map to the 2900-7000 K range already used by the UI.
- `PUT /elgato/lights` is still the correct write path for power, brightness, and temperature, with read-back after the write.
- `GET /elgato/lights/settings` returns transition/default settings: `colorChangeDurationMs`, `switchOnDurationMs`, `switchOffDurationMs`, `powerOnBehavior`, `powerOnBrightness`, and `powerOnTemperature`.
- `GET /elgato/battery-info`, `GET /elgato/scenes`, `GET /elgato/lights/scenes`, and `GET /elgato/lightstrip` returned `404` on Key Light Air. That means scene and battery UI must be capability-gated, not assumed globally.

The Light Strip Pro was not discoverable during this investigation, so RGB/scene LAN writes remain unverified on the real strip. The previous spike saw a Light Strip Pro report `temperature=0`; that still supports treating it as not color-temperature-capable, but it is not enough to ship RGB or scene control without a fresh live probe.

### Wrapper decision

Keep extending the direct client for the next PR. `python-elgato` is useful as protocol reference, but adopting it now would add a dependency while still leaving the app to own discovery, display-name persistence, per-device capability shaping, API serialization, and PWA UX. Its useful deltas for this repo are small and directly portable:

- `GET /elgato/lights/settings`
- optional `GET /elgato/battery-info`
- `PUT /elgato/lights` with `hue` / `saturation` for Light Strip
- optional `POST /elgato/identify` and `POST /elgato/restart` if we want guarded diagnostics later
- `PUT /elgato/accessory-info` for vendor display-name writes, which should stay out of scope because this app already has local labels

Reconsider adopting the wrapper only if scene upload/activation turns out to require more protocol surface than the simple `/elgato/lights` and `/elgato/lights/settings` calls.

### Not yet built

The parity work these findings point at — hue/saturation for the Light Strip, battery status for the Key Light Mini, the opportunistic `/elgato/lights/settings` + `/elgato/battery-info` probes, and the capability flags that gate them — is tracked in [#638](https://github.com/ferraroroberto/home-automation/issues/638), not here. This document stays what it is: the spike result, the verified LAN API shape, and the wrapper decision record.

## Verification

Read all discoverable lights:

```powershell
& .\.venv\Scripts\python.exe -m src.list_elgato_lights
```

Write a light and read back the accepted state:

```powershell
& .\.venv\Scripts\python.exe -m src.list_elgato_lights --id <host>:9123 --on off --brightness 15 --temperature 203
```
