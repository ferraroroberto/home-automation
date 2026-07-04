# Architecture — per-module reference

The exhaustive module-by-module inventory of the repository. The [README](../README.md#layout) carries the top-level directory map (what each directory *is*); this file is the detailed catalogue (what each file *does*). Keep the two in sync when a module is added, split, or removed — the README map is a dozen lines, this is the full list.

## `src/` — non-UI Python

- `melcloud_client.py` — async auth + fetch + control (the shared core).
- `list_devices.py` — CLI that prints each unit's live state.
- `sma_client.py` — async read of the local SMA solar/energy devices (meter + inverter).
- `list_energy.py` — CLI that prints the live energy flow.
- `energy_history.py` — SQLite store + rollups for the energy dashboard history.
- `telemetry.py` — unified event + reading store (SQLite; #283/#289). Narrow `readings`/`events` tables with JSON sidecars; producers (alarm/power/presence/plug/RISCO) mirror their events in, the Home **Activity log** reads them back.
- `hvac_automation.py` — UI-free persistence + control law for per-unit dynamic temperature rules and daily schedules.
- `tariff.py` — electricity tariff model: prices grid energy per time-of-use period and values self-consumed PV (the cost & savings breakdown). UI-free, graceful flat-rate default.
- `tuya_client.py` — Smart Life / Tuya discovery and local LAN control foundation.
- `ups_client.py` — local USB UPS status reader: prefers NUT (`upsc` against the portable Windows install), falls back to a one-shot NUT USB-HID probe, then Windows `Win32_Battery`.
- `host_shutdown.py` — thin wrapper over Windows `shutdown.exe`: `initiate_shutdown()` schedules a graceful shutdown with a grace window, `cancel_shutdown()` aborts a pending one. Hard-blocked under pytest so tests can never trigger a real shutdown. Used by the UPS low-battery safety trigger (see `power_monitor.py`).
- `hyperv_client.py` — status + lifecycle control for the Home Assistant Hyper-V VM (#240): shells out to `Get-VM` / `Start-VM` / `Stop-VM` for the single VM named by `HA_VM_NAME` (by name only — never any other VM), returning a flattened `HyperVState` (state, uptime, IP, MAC). Distinct errors for missing name / not-found / insufficient rights / already-in-state; graceful ACPI stop only.
- `elgato_client.py` — Elgato lights discovery/read/control over the local LAN HTTP API.
- `_atomic_json.py` — shared atomic write-tmp + `os.replace` primitives (`atomic_write_bytes` / `write_json_atomic`; issue #327), the single implementation behind the pattern that used to be copy-pasted across 18 `src/` modules.
- `_schedule_store.py` — shared read/save + `safe_id`/`clean_time`/`clean_days` helpers for weekly schedule-entry stores (issue #327), deduping `security_schedules.py` and `wake_alarms.py` (`hvac_automation.py` reuses the read/save pair too); built on `_atomic_json.py`.
- `_toggle_prefs.py` — parametrized load/save for frozen bool-toggle-dataclass prefs stores (issue #327), deduping `alarm_notify_prefs.py` and `power_notify_prefs.py`; built on `_atomic_json.py`.
- `risco_client.py` — async RISCO Cloud alarm state (incl. system AC-power + per-zone trouble flags), controls, event log, and detector bypass.
- `security_schedules.py` — UI-free persistence and due-window checks for weekly alarm schedules; read/save + field-cleaning via `_schedule_store.py`.
- `wake_alarms.py` — UI-free persistence + due-window checks for **wake alarms** (recurring day-of-week or one-shot date; Alexa-style, ring/notify only — never touches the RISCO security alarm), plus the voice helpers `parse_spoken_alarm` (spoken time/schedule → entry), `next_fire`/`soonest_enabled`, and `describe_alarm` (speakable summary) used by the voice API (#306). Shares `security_schedules.py`'s read/save + field-cleaning via `_schedule_store.py`; `config/wake_alarms.json`.
- `wake_timers.py` — in-memory (unpersisted) countdown-**timer** store; mirrors Home Assistant's own ephemeral voice timers (a restart clears them). Create/list/cancel + `mark_expired`.
- `presence_client.py` — read-only iCloud Find My spike client for location/presence feasibility.
- `network_client.py` — async home-network orchestrator (issue #197 split): imports the three sub-modules below and exposes the unchanged public surface (`fetch_network_state`, `resolve_ip_by_mac`, dataclasses) so callers don't move.
- `network_types.py` — shared dataclasses, exceptions, and leaf helpers used across all network sub-modules.
- `network_ap.py` — NETGEAR R9000 AP: device inventory (MAC/IP/name/signal/band/SSID), AP health, `reboot_access_point`, MAC rediscovery.
- `network_router.py` — Vodafone ZXHN F6600P (ZTE): SHA256-challenge login, WAN/DHCP reads, binding write-back, `reboot_router`.
- `network_host.py` — host-side internet probes: ping latency + packet loss, optional speedtest, `netsh wlan`.
- `list_network.py` — CLI that prints the live network state and inventory.
- `dhcp_plan.py` — UI-free, network-free DHCP reservation planner (#170): classifies each device into a category range from `config/dhcp_plan.json` and assigns the lowest free IP, anchoring stability on the router's existing bindings and tagging each row reserved/create/change; warns on overflow/overlap/unclassified/randomised-MAC.
- `list_dhcp_plan.py` — CLI that prints the categorised reservation plan (mirrors `list_network.py`); `--apply` pushes the create/change rows to the router (#176) behind a `yes` prompt.
- `network_display_names.py` / `network_wifi_display_names.py` / `network_hidden.py` — Network-tab label and hidden-state stores for attached devices and Wi-Fi radios; reuse `display_names.py` atomic load/save/set verbatim, parallel to the unit/plug/detector stores. `network_hidden.py` covers both attached-device (`config/network_hidden.json`) and Wi-Fi radio (`config/network_wifi_hidden.json`) hidden state in one module.
- `network_oui.py` — offline device identification: bundled trimmed OUI→vendor table, randomised-MAC detection, and a category/icon heuristic (no network call, render-time).
- `network_history.py` — per-MAC history store (SQLite, modeled on `energy_history.py`; issue #129 Phase 4): first/last/times-seen, the `important` flag, and the online/offline + new-device derivations. Recorded on each `/api/network` read (no background sampler — the AP read is expensive and tab-gated); randomised MACs are never tracked. Kept separate from the rename/hidden stores; gitignored `webapp/network_history.sqlite3`.
- `dhcp_overrides.py` — per-MAC category overrides chosen in the Network tab UI; persisted to gitignored `config/dhcp_overrides.json` and merged over `config/dhcp_plan.json` by the planner so a UI choice wins without editing the committed config.
- `camera_client.py` — async, UI-free camera core (issue #161): ONVIF for discovery/profiles/PTZ (`ContinuousMove` / `Stop` / `AbsoluteMove`, preset management), RTSP + ffmpeg for snapshot / MJPEG stream / clip recording. Returns a flattened `CameraInfo` per declared camera; capabilities (`ptz_presets`, `ptz_absolute`, `ptz_relative`) gate the UI controls. Cameras declared in gitignored `config/cameras.json`.
- `camera_ffmpeg.py` — ffmpeg subprocess wrapper used by `camera_client.py` for RTSP→JPEG snapshot, RTSP→MJPEG stream transcoding, and server-side mp4 recording.
- `camera_display_names.py` — custom label store for camera IDs; parallel to `display_names.py`; persisted to gitignored `config/camera_display_names.json`.
- `camera_presets.py` — software PTZ preset store for cameras that lack native ONVIF preset support; persisted to gitignored `config/camera_presets.json` (absolute pan/tilt/zoom coordinates).
- `camera_preset_names.py` — custom label store for camera preset tokens; persisted to gitignored `config/camera_preset_names.json`.
- `pv_forecast.py` — Open-Meteo global-tilted-irradiance → expected-generation forecast engine (issue #39): one keyless API call per request for yesterday/today/tomorrow; scales hourly GTI by kWp × performance_ratio. UI-free; read by `GET /api/energy/forecast`.
- `pv_system_config.py` — PV array config loader (`config/pv_system.json`): kWp, tilt, azimuth, performance ratio. Missing file returns a disabled sentinel rather than an error.
- `location_config.py` — home-location config loader (`config/location.json`): lat/lon/optional label used by the weather and PV-forecast endpoints. Missing file surfaces as `configured=False` with HTTP 200 — never a 500.
- `elgato_display_names.py` — custom label store for Elgato lights; keyed by MAC address when available (survives DHCP changes), with a legacy host:port fallback; persisted to gitignored `config/elgato_display_names.json`.
- `tuya_hidden.py` — hidden-state store for Tuya plugs/blinds; persisted to gitignored `config/tuya_hidden.json`; parallel to `network_hidden.py` and `security_hidden.py`.
- `security_hidden.py` — hidden-state store for RISCO detectors; persisted to gitignored `config/security_hidden.json`.
- `security_trouble_ignore.py` — per-zone trouble-ignore store; persisted to gitignored `config/security_trouble_ignore.json`; a muted zone is dropped from the main-card trouble count while still showing `Trouble — ignored` in the list.
- `presence_display_names.py` — custom label store for presence person IDs; parallel to `display_names.py`; persisted to gitignored `config/presence_display_names.json`.
- `presence_hidden.py` — hidden-state store for Find My / presence entities; persisted to gitignored `config/presence_hidden.json`.
- `presence_engine.py` — webhook-backed presence state and alarm-transition decision engine: reads `config/presence_state.json`, evaluates grace-period / Kids-home logic, and drives arm/disarm; appends a JSONL audit row to gitignored `logs/presence_triggers.jsonl`.
- `push_notifications.py` — best-effort Web Push sender for presence transitions: reads VAPID keys + subscriptions from gitignored `config/push_config.json` / `config/push_subscriptions.json`; silent no-op when not configured.
- `notify/` — the universal Telegram notifier vendored verbatim from `project-scaffolding` (`TelegramNotifier` + `NotifierError` + `TelegramConfig` + `build_notifier`); `notify_config.py` wires the app's gitignored `config/notify_config.json` / `TELEGRAM_*` env to it (`build_alarm_notifier()` → notifier or silent no-op).
- `activity_log.py` — reusable append-only JSONL activity log; `append_activity(consumer, event)` writes one timestamped line to gitignored `logs/<consumer>.jsonl`. Used for `logs/alarm.jsonl` (every arm/disarm command + result) and delegated to by the presence trigger log.
- `alarm_notify_prefs.py` — seven per-event toggles for alarm Telegram alerts (arm/disarm successes default off; `error`/`intrusion`/`ac_lost` default on), persisted to gitignored `config/alarm_notify_prefs.json` via `_toggle_prefs.py`.
- `power_notify_prefs.py` — three toggles (`power_lost` / `power_restored` / `auto_shutdown_low_battery`, all default on) for UPS mains-power Telegram alerts and the low-battery safety auto-shutdown, persisted to gitignored `config/power_notify_prefs.json` via `_toggle_prefs.py`.
- `list_security.py` — CLI that prints the live RISCO alarm state (mirrors `list_devices.py`).
- `list_elgato_lights.py` — CLI that prints + controls Elgato lights; supports `--id`, `--on`, `--brightness`, `--kelvin`.
- `list_cameras.py` — CLI that prints camera reachability and last-snapshot metadata.
- `list_presence.py` — CLI that prints visible iCloud Find My presence entities; accepts `--2fa-code` for fresh session auth.
- `webapp_config.py` — webapp host/port + auth secrets loader.
- `static_versioning.py` — build identity (git SHA) + content-hash (`?v=`) stamping of the PWA's `.js`/`.css` URLs so a mobile PWA never serves stale cached code.

## `app/webapp/` — the FastAPI + PWA product

- `server.py` — `create_app()`, middleware, caching static mount, routers, background-task lifespan.
- `middleware.py` — bearer-token / loopback auth gate.
- `manager.py` — adopt-or-spawn / restart / stop for the uvicorn webapp (used by the tray).
- `sampler.py` — background energy sampler owned by the webapp lifecycle.
- `automation.py` — background HVAC automation evaluator (dynamic setpoint rules + schedules) owned by the webapp lifecycle.
- `security_automation.py` — background weekly alarm-schedule evaluator owned by the webapp lifecycle.
- `wake_alarm_automation.py` — background wake-alarm + timer evaluator owned by the webapp lifecycle: fires due alarms (marks "ringing", best-effort Telegram notify, rearms weekly / auto-disables one-shot) and expires due timers.
- `presence_automation.py` — background presence → alarm automation consumer (evaluates webhook-backed presence state and fires arm/disarm) owned by the webapp lifecycle.
- `alarm_notify.py` — single `record_alarm_action()` entry point used by the schedule, presence, and manual arm/disarm paths: always appends to the `logs/alarm.jsonl` activity log, and sends a Telegram alert only for automatic sources whose toggle is on (manual never notifies; errors de-dupe to once/day). Also `record_security_event()` / `check_security_transitions()` for edge-triggered intrusion + panel-AC alerts off the presence loop's RISCO read.
- `power_monitor.py` — background task (webapp lifespan) that polls the UPS every `POWER_MONITOR_POLL_INTERVAL_S` in a worker thread, fires `power_notify.record_power_event()` on each mains↔battery transition (the server-side watcher behind the power-loss Telegram alerts), and enforces the low-battery safety shutdown: edge-triggers `power_notify.record_low_battery_shutdown()` once per outage when on-battery runtime drops to 15 min or below (fires even on the monitor's first observation, unlike the mains-transition alerts), cancelling via `record_low_battery_shutdown_cancelled()` if mains power returns first.
- `power_notify.py` — `record_power_event()`: appends UPS power transitions to `logs/power.jsonl` and sends a Telegram alert per the `power_lost`/`power_restored` toggle. `record_low_battery_shutdown()` / `record_low_battery_shutdown_cancelled()`: the safety-net pair — always logs, and when `auto_shutdown_low_battery` is on, sends a distinct Telegram alert and calls `src.host_shutdown` to schedule/cancel the OS shutdown.
- `presence_refresher.py` — bounded background iCloud Find My diagnostic refresher; browser polling reads its in-memory cache via `GET /api/presence`, so the expensive Apple call happens only here.
- `routers/` — `units` (read + control), `energy` (live flow + history/aggregate + cost breakdown), `tuya` (local Smart Life devices + watts), `ups` (local USB UPS status), `lights` (Elgato lights), `security` (RISCO alarm state/control), `network` (LAN health + device inventory + AP reboot), `hyperv` (Home Assistant VM status + start/stop), `cameras` (ONVIF/RTSP cameras — snapshot, MJPEG stream, PTZ, presets, recording), `presence` (presence/location webhooks + diagnostics), `weather` (Open-Meteo weather + forecast), `push` (Web Push subscription management), `wake_alarms` (wake-alarm CRUD + test/dismiss and the in-memory countdown timers), `auth` (login), `misc` (page, health, build identity), `nav_debug` (on-device nav-pin diagnostic sink).
- `static/` — the PWA (HTML/CSS/ES-modules), `manifest.webmanifest`, icons.
  Modules: `main.js` (boot + AC cards), `tabs.js` (Home/AC/Energy/Plugs/Light/Net/Alarm switcher),
  `energy.js` (energy tab + live polling), `plugs.js` (Smart Life tab), `ups.js` (local USB UPS tile + outage/restored toasts), `ups-notify.js` (UPS power-event Telegram toggles), `lights.js` (Elgato tab),
  `security.js` (RISCO alarm tab boot/orchestrator), `security-alarm.js` (alarm state + action pills + detectors), `security-schedules.js` (weekly schedule CRUD), `security-notify.js` (automatic-alarm Telegram notification toggles), `cameras.js` (camera tile — thumbnails, live MJPEG view, PTZ, presets, recording), `presence.js` (presence card + location + automation + push),
  `network.js` (Network/LAN tab boot/orchestrator), `network-devices.js` (attached-devices list + modal), `network-wifi.js` (Wi-Fi diagnostics + charts), `network-dhcp.js` (DHCP reservation planner),
  `vm.js` (Home Assistant Hyper-V VM tile — last Home card, status + start/stop), `wake-alarms.js` (Home-card wake-alarm + timer CRUD, styled like the Security schedule card), `weather.js` (weather strip + theme toggle),
  `snapshots.js` (allowlisted last-good browser snapshots), `charts.js` (Chart.js wrappers), `state.js`, `api.js`, `icons.js` (icon helpers), `scroll-lock.js` (body scroll lock for modals), `nav-debug.js` (on-device nav-pin diagnostic — toggled via the gauge icon next to the theme toggle; posts events to `POST /api/nav-debug`, appended to the gitignored `webapp/nav_debug.log`, issue #300), `sw.js` (service worker);
  `vendor/chart.umd.min.js` (vendored Chart.js v4).

## `app/tray/` — the Windows tray that owns the webapp lifecycle (`tray.bat`)

- `tray.py` — pystray icon + menu; `__main__.py` — the `-m app.tray` entry.
- `single_instance.py`, `tray_lifecycle.ps1` — vendored verbatim from the scaffold.

## `custom_components/home_automation_app/` — Home Assistant custom integration (#235)

A thin adapter over the existing `/api/*` endpoints that exposes native `climate`, `switch`, `alarm_control_panel`, `binary_sensor`, and `sensor` entities. See [`docs/home-assistant-integration/`](home-assistant-integration/README.md).

## `scripts/`

`gen_tailscale_cert.py` (HTTPS via `tailscale cert`, `--check` auto-renew), `gen_token.py` / `set_password.py` (auth), `gen_web_push_keys.py` (generate local VAPID keys for Web Push), `gen_icons.py` (PWA icons; Pillow dev-only), `ha_config_sync.py` (deploy the voice-PE HA config into the HA VM's `/config` over SSH — preflight / deploy / rollback / probe; #243, see [Home Assistant config deploy](../README.md#home-assistant-config-deploy-over-ssh)).

## `spike/`

`streamlit_app.py`, the independent POC spike.

## `config/`

Committed samples: `webapp_config.sample.json`, `display_names.sample.json`, `tuya_display_names.sample.json`, `tuya_hidden.sample.json`, `elgato_display_names.sample.json`, `network_display_names.sample.json`, `network_hidden.sample.json`, `network_wifi_display_names.sample.json`, `network_wifi_hidden.sample.json`, `security_display_names.sample.json`, `security_hidden.sample.json`, `security_trouble_ignore.sample.json`, `security_schedules.sample.json`, `wake_alarms.sample.json`, `presence_display_names.sample.json`, `presence_hidden.sample.json`, `presence_state.sample.json`, `presence_automation.sample.json`, `push_config.sample.json`, `notify_config.sample.json`, `alarm_notify_prefs.sample.json`, `power_notify_prefs.sample.json`, `hvac_rules.sample.json`, `hvac_schedules.sample.json`, `location.sample.json`, `tariff.sample.json`, `pv_system.sample.json`, `cameras.sample.json`, `camera_display_names.sample.json`, `camera_presets.sample.json`, `camera_preset_names.sample.json`, `dhcp_plan.sample.json`, and `dhcp_overrides.sample.json`. The real counterparts (`webapp_config.json`, `display_names.json`, `tuya_display_names.json`, `tuya_hidden.json`, `elgato_display_names.json`, `network_display_names.json`, `network_hidden.json`, `network_wifi_display_names.json`, `network_wifi_hidden.json`, `security_display_names.json`, `security_hidden.json`, `security_trouble_ignore.json`, `security_schedules.json`, `wake_alarms.json`, `presence_display_names.json`, `presence_hidden.json`, `presence_state.json`, `presence_automation.json`, `push_config.json`, `push_subscriptions.json`, `notify_config.json`, `alarm_notify_prefs.json`, `power_notify_prefs.json`, `hvac_rules.json`, `hvac_schedules.json`, `location.json`, `tariff.json`, `pv_system.json`, `cameras.json`, `camera_display_names.json`, `camera_presets.json`, `camera_preset_names.json`, `dhcp_plan.json`, and `dhcp_overrides.json`) are gitignored.

## `webapp/`

Runtime state (`certificates/`, `auth.log`, `energy_history.sqlite3`, `telemetry.sqlite3`); gitignored.

## `.env`

MELCloud + SMA credentials (gitignored; copy from `.env.example`).
