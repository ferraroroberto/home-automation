# home-automation

Control your Mitsubishi Electric units from your phone — a mobile-first,
installable **PWA** over **MELCloud Home**, ahead of building a solar
load-balancing automation on top of it.

> **Platform note.** These units migrated from classic MELCloud
> (`app.melcloud.com`) to **MELCloud Home**, which is a different API. The
> classic `pymelcloud` library cannot see them. This project uses
> [`aiomelcloudhome`](https://github.com/erwindouna/aiomelcloudhome) — a
> pure-async client that does the PKCE login over HTTP (no browser). Use
> your **MELCloud Home** credentials in `.env`.

The product is a **FastAPI + static PWA**: a card grid showing every unit at
once, each card carrying the everyday controls inline (on/off, target
temperature, fan speed, room-temperature readout); a per-unit detail modal
holds the rest (operation mode + the two vanes). It is reachable on the LAN
and over **Tailscale**, behind a self-signed-CA HTTPS endpoint and an
optional bearer token. Three ways to reach it once running:

- **Local** (same Wi-Fi): `https://<pc-hostname>:8447`
- **Tailscale** (anywhere on the tailnet): `https://<pc>.<tailnet>.ts.net:8447`
- **Loopback** (the PC itself): `https://127.0.0.1:8447`

> The **Streamlit app is a POC spike** (`spike/streamlit_app.py`) — a
> throwaway data/debug view, independent from the product. See
> [Streamlit spike](#streamlit-spike-poc).

## Layout

- **`src/`** — non-UI Python.
  - `melcloud_client.py` — async auth + fetch + control (the shared core).
  - `list_devices.py` — CLI that prints each unit's live state.
  - `sma_client.py` — async read of the local SMA solar/energy devices (meter + inverter).
  - `list_energy.py` — CLI that prints the live energy flow.
  - `energy_history.py` — SQLite store + rollups for the energy dashboard history.
  - `hvac_automation.py` — UI-free persistence + control law for per-unit dynamic temperature rules and daily schedules.
  - `tariff.py` — electricity tariff model: prices grid energy per time-of-use period and values self-consumed PV (the cost & savings breakdown). UI-free, graceful flat-rate default.
  - `tuya_client.py` — Smart Life / Tuya discovery and local LAN control foundation.
  - `elgato_client.py` — Elgato lights discovery/read/control over the local LAN HTTP API.
  - `risco_client.py` — async RISCO Cloud alarm state (incl. system-wide low-battery + per-zone trouble flags), controls, event log, and detector bypass.
  - `security_schedules.py` — UI-free persistence and due-window checks for weekly alarm schedules.
  - `presence_client.py` — read-only iCloud Find My spike client for location/presence feasibility.
  - `network_client.py` — async home-network spike core: internet/AP/router health + attached-device inventory + AP reboot (issue #125).
  - `list_network.py` — CLI that prints the live network state and inventory.
  - `network_display_names.py` / `network_wifi_display_names.py` / `network_hidden.py` — Network-tab label and hidden-state stores for attached devices and Wi-Fi radios; reuse `display_names.py` atomic load/save/set verbatim, parallel to the unit/plug/detector stores.
  - `network_oui.py` — offline device identification: bundled trimmed OUI→vendor table, randomised-MAC detection, and a category/icon heuristic (no network call, render-time).
  - `network_history.py` — per-MAC history store (SQLite, modeled on `energy_history.py`; issue #129 Phase 4): first/last/times-seen, the `important` flag, and the online/offline + new-device derivations. Recorded on each `/api/network` read (no background sampler — the AP read is expensive and tab-gated); randomised MACs are never tracked. Kept separate from the rename/hidden stores; gitignored `webapp/network_history.sqlite3`.
  - `webapp_config.py` — webapp host/port + auth secrets loader.
  - `static_versioning.py` — build identity (git SHA) + content-hash (`?v=`) stamping of the PWA's `.js`/`.css` URLs so a mobile PWA never serves stale cached code.
- **`app/webapp/`** — the FastAPI + PWA product.
  - `server.py` — `create_app()`, middleware, caching static mount, routers, background-task lifespan.
  - `middleware.py` — bearer-token / loopback auth gate.
  - `manager.py` — adopt-or-spawn / restart / stop for the uvicorn webapp (used by the tray).
  - `sampler.py` — background energy sampler owned by the webapp lifecycle.
  - `automation.py` — background HVAC automation evaluator (dynamic setpoint rules + schedules) owned by the webapp lifecycle.
  - `security_automation.py` — background weekly alarm-schedule evaluator owned by the webapp lifecycle.
  - `routers/` — `units` (read + control), `energy` (live flow + history/aggregate + cost breakdown), `tuya` (local Smart Life devices + watts), `lights` (Elgato lights), `security` (RISCO alarm state/control), `network` (LAN health + device inventory + AP reboot), `auth` (login), `misc` (page, health, CA profile).
  - `static/` — the PWA (HTML/CSS/ES-modules), `manifest.webmanifest`, icons.
    Modules: `main.js` (boot + AC cards), `tabs.js` (Home/AC/Energy/Plugs/Light/Net/Alarm switcher),
    `energy.js` (energy tab + live polling), `plugs.js` (Smart Life tab), `lights.js` (Elgato tab),
    `security.js` (RISCO alarm tab), `network.js` (Network/LAN tab + reusable confirm dialog),
    `snapshots.js` (allowlisted last-good browser snapshots), `charts.js` (Chart.js wrappers), `state.js`, `api.js`;
    `vendor/chart.umd.min.js` (vendored Chart.js v4).
- **`app/tray/`** — the Windows tray that owns the webapp lifecycle (`tray.bat`).
  - `tray.py` — pystray icon + menu; `__main__.py` — the `-m app.tray` entry.
  - `single_instance.py`, `tray_lifecycle.ps1` — vendored verbatim from the scaffold.
- **`scripts/`** — `gen_ssl_cert.py` (HTTPS CA+leaf), `gen_token.py` / `set_password.py` (auth), `gen_icons.py` (PWA icons).
- **`spike/`** — `streamlit_app.py`, the independent POC spike.
- **`config/`** — `webapp_config.sample.json`, `display_names.sample.json`, `tuya_display_names.sample.json`, `elgato_display_names.sample.json`, `network_hidden.sample.json`, `network_wifi_display_names.sample.json`, `network_wifi_hidden.sample.json`, `security_display_names.sample.json`, `security_hidden.sample.json`, `security_schedules.sample.json`, `presence_display_names.sample.json`, `presence_hidden.sample.json`, `presence_state.sample.json`, `presence_automation.sample.json`, `push_config.sample.json`, `hvac_rules.sample.json`, `hvac_schedules.sample.json`, `location.sample.json`, `tariff.sample.json`, and `pv_system.sample.json` committed; the real `webapp_config.json`, `display_names.json`, `tuya_display_names.json`, `elgato_display_names.json`, `network_display_names.json`, `network_hidden.json`, `network_wifi_display_names.json`, `network_wifi_hidden.json`, `security_display_names.json`, `security_hidden.json`, `security_schedules.json`, `presence_display_names.json`, `presence_hidden.json`, `presence_state.json`, `presence_automation.json`, `push_config.json`, `push_subscriptions.json`, `hvac_rules.json`, `hvac_schedules.json`, `location.json`, `tariff.json`, and `pv_system.json` are gitignored.
- **`webapp/`** — runtime state (`certificates/`, `auth.log`, `energy_history.sqlite3`); gitignored.
- **`.env`** — MELCloud + SMA credentials (gitignored; copy from `.env.example`).

## Setup

The virtual environment lives at `.venv`. Install dependencies:

```powershell
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt   # Windows
```

```bash
./.venv/bin/python -m pip install -r requirements.txt             # POSIX
```

## Configure credentials

```powershell
Copy-Item .env.example .env      # Windows
```

Then edit `.env` and set `MELCLOUD_EMAIL` and `MELCLOUD_PASSWORD` (your
MELCloud Home login).

## RISCO alarm / Security tab

The **Security** tab integrates the RISCO Cloud alarm through `pyrisco` for
state/events and the native RISCO WebUI command path for arm/disarm actions.
It shows the current alarm state as a single centered `Alarm state: <Word>`
line (colour-coded with the three-colour scheme below), one row of rounded
action pills (`Disarm` / `Partial` / `Perimeter` / `Full`), the recent event
log, and a collapsible detector list with per-zone toggles (active = green,
bypassed = red). The same alarm state + action pills are mirrored, actionable,
on the **Home** tab.

**Low-battery alert + detector data (issue #84).** When the panel reports a
system-wide low battery, an amber **`⚠ Low battery`** badge appears on the
`Alarm state` line on **both** the Home and Security tiles and clears when the
flag is cleared. (The RISCO Cloud API does not expose per-detector battery —
only this aggregate flag and a generic per-zone *trouble* boolean — so the badge
is a "something needs attention → drill in" signal, not a per-detector readout.)

**AC-power-lost alert (issue #99).** When the panel loses mains power and runs on
backup battery, a red **`⚠ AC power lost`** badge appears on the same `Alarm
state` line (Home + Security tiles) from the aggregate `ac_lost` flag, mirroring
the low-battery badge and clearing when mains power returns. The red tint (vs the
amber low-battery badge) marks the higher urgency; both can show together.

Each detector row shows its flags inline (`Active`/`Bypass`/`Triggered` in their
state colour, with `Trouble` always in the amber attention colour); the list is
sorted A–Z by label. Tapping a detector's name opens a detail modal showing its
type, status, and trouble state, plus a **Display name** field with the original
RISCO **system name** shown beneath it for correspondence. Detectors arrive named
`1`, `2`, … so a custom label is saved via `PUT /api/security/zones/{id}/display_name`
to a gitignored `config/security_display_names.json` (zone id → label, parallel to
the unit `config/display_names.json` and plug `config/tuya_display_names.json`); a
missing file is not an error, and the override wins over the RISCO name everywhere.

**Hiding unused detectors (issue #104).** The detail modal also has a **Hidden**
toggle that parks an unused detector out of the default list, saved via
`PUT /api/security/zones/{id}/hidden` to a gitignored `config/security_hidden.json`
(reusing the same atomic store as the display-name files). The Detectors card
header then shows an `N hidden` counter and a **Show hidden / Hide** switch that
reveals the hidden detectors (dimmed) on demand so they can be un-hidden.

**Resilient live read (issue #98).** A momentary panel-unreachable blip — RISCO
returns a non-retryable result code such as `26` on the *live* state read even
though the login, site, and PIN steps all succeed — no longer blacks out the
tab. `fetch_security_state()` falls back to the **cloud-cached** snapshot
(`fromControlPanel=False`), so the alarm state, the detector list, and the
low-battery/trouble flags keep rendering and the action pills stay actionable.
The response is flagged `assumed_control_panel_state=true` to mark it as cached
rather than a fresh live read, an info-level breadcrumb logs each fallback, and
only a failure of *both* the live and cached reads surfaces as an error. This
mirrors the SMA stale-cloud energy fallback (issues #94 / #95). The write paths
(arm/disarm, bypass) are unchanged — a live read after a command is correct
there.

The pills use translucent colour tints in three identities — **green** Disarm,
**yellow** Partial/Perimeter, **red** Full — and only the actions you can
actually take carry colour: when disarmed the three arm options are active and
Disarm fades; when armed only Disarm is active. The current state is conveyed by
the `Alarm state` line, not by a highlighted pill. Tapping Disarm also clears a
trouble/alarm-memory condition. Every action shows an optimistic neutral
frosted toast on tap, then re-renders from the panel's live state.

**Weekly alarm schedules.** The Alarm tab includes a collapsible **Schedules**
card for multiple local weekly schedule entries. Each entry can be enabled,
assigned to any weekday combination, given a time, and set to `Disarm`,
`Partial`, `Perimeter`, or `Full`. The tray-owned webapp evaluates these entries
server-side, fires each entry at most once per calendar day, and logs failed
alarm commands without stopping the scheduler so a transient RISCO error can be
retried on the next poll. The entries live in gitignored
`config/security_schedules.json`; copy `config/security_schedules.sample.json`
for the persisted shape if editing by hand.

Config in `.env`:

| Key | Meaning |
|-----|---------|
| `RISCO_USERNAME` | RISCO Cloud login email. Use a dedicated sub-account if possible so the dashboard does not compete with the phone app session. |
| `RISCO_PASSWORD` | RISCO Cloud password. |
| `RISCO_PIN` | Panel PIN used by RISCO Cloud/WebUI commands. |
| `RISCO_PERIMETER_GROUP` | Optional group letter used only to label partially-set states more precisely. |
| `RISCO_PARTIAL_GROUP` | Optional group letter used only to label partially-set states more precisely. |
| `SECURITY_SCHEDULES_ENABLED` | Optional, default `true`; set `0` to disable the weekly alarm-schedule evaluator while keeping the UI/API available. |
| `SECURITY_SCHEDULES_POLL_INTERVAL_S` | Optional, default `60`; how often the tray-owned webapp checks due alarm schedules. |

Read-only smoke command:

```powershell
& .\.venv\Scripts\python.exe -m src.list_security
```

RISCO periodically blocks third-party clients. If login starts failing after
credentials were known-good, check for a newer `pyrisco` release before changing
the app code.

## iCloud Find My presence spike

Issue #86 adds a read-only spike for Apple Find My as a possible presence source for later HVAC automation. There is no official Apple Find My API; this repo uses `pyicloud` against iCloud's web Find My surface to test whether the data is live enough and operationally stable enough. This is a feasibility read, not an automation input yet.

Config in `.env`:

| Key | Meaning |
|-----|---------|
| `ICLOUD_EMAIL` | Apple Account email. |
| `ICLOUD_PASSWORD` | Apple Account password. |
| `ICLOUD_SESSION_DIR` | Optional session/cookie cache directory. Default: `webapp/icloud_session`. This contains live Apple auth material and is gitignored. |
| `PRESENCE_HOME_RADIUS_M` | Radius around `config/location.json` used to classify located devices as home or away. Default: `200`. |

Run the smoke command:

```powershell
& .\.venv\Scripts\python.exe -m src.list_presence
```

On a fresh or expired session Apple may require 2FA. If the command reports that, approve the sign-in on a trusted Apple device and rerun with the displayed code:

```powershell
& .\.venv\Scripts\python.exe -m src.list_presence --2fa-code 123456
```

The CLI prints each visible Find My entity's name, model/class, coordinates, accuracy, last-seen time, battery, and distance from `config/location.json` when the home location is configured. The app also exposes the same read as `GET /api/presence` and renders a minimal **Presence** card in the Security tab showing home / away / unknown counts plus the entity rows. Do not commit Apple credentials, session cookies, person names, coordinates, or location dumps; this repository is public.

Spike recommendation: use this read path only if the session survives unattended for weeks and returns the two phones reliably. If 2FA/session expiry or missing shared-object data proves brittle, use iOS Shortcuts arrive/leave webhooks for the actual presence trigger and keep this client as an exploratory diagnostic. The follow-up HVAC actions should be separate: everyone away for a grace period powers off idle units; first arrival restores a saved profile. Lighting remains out of scope because this repo has no lighting backend.

## Presence webhooks, home location, and alarm automation

Presence automation uses iOS Shortcuts webhooks as the write path. Find My/iCloud is now only a bounded server-side diagnostic refresh for distance/status enrichment, so opening the Security tab every few seconds does not perform a fresh Apple locate call.

Set a shared webhook secret in `.env`:

| Key | Meaning |
|-----|---------|
| `PRESENCE_WEBHOOK_SECRET` | Secret required by the iOS Shortcuts webhook endpoints. |
| `PRESENCE_ICLOUD_REFRESH_ENABLED` | Optional, default `true`; set `0` to disable the cached Find My diagnostic refresher. |
| `PRESENCE_ICLOUD_REFRESH_INTERVAL_S` | Optional, default `300`; minimum 60 seconds between Find My diagnostic refreshes. |
| `PRESENCE_AUTOMATION_ENGINE_ENABLED` | Optional, default `true`; the persisted automation config still defaults off. |
| `PRESENCE_AUTOMATION_POLL_INTERVAL_S` | Optional, default `10`; how often the alarm consumer evaluates webhook-backed state. |

Shortcut endpoints:

```powershell
POST https://<host>:8447/api/presence/webhooks/roberto/home
Authorization: Bearer <PRESENCE_WEBHOOK_SECRET>
```

Use `home` for the Arrive automation and `away` for the Leave automation. Create one stable person id per phone, for example `roberto` and `ana`. The Security tab's Presence card can rename those ids, hide non-household Find My entities behind Show hidden, edit `config/location.json`, and set the alarm automation thresholds. The automation defaults off and only acts on fresh webhook-backed people that are not hidden.

Set up each iPhone with two Personal Automations in Shortcuts:

1. **Arrive**: Shortcuts → Automation → New Automation → Arrive → choose the home geofence → Run Immediately → Get Contents of URL.
2. URL: `https://<host>:8447/api/presence/webhooks/<person_id>/home`; method: `POST`; header key: `Authorization`; header value: `Bearer <PRESENCE_WEBHOOK_SECRET>`. Do **not** use the dashboard `?token=` URL parameter here — the webhook uses its own secret.
3. **Leave**: duplicate the automation with URL `https://<host>:8447/api/presence/webhooks/<person_id>/away`.
4. Repeat with a different stable `<person_id>` for the other phone, for example `ana`.

To test immediately, run the same **Get Contents of URL** action from a temporary normal Shortcut (or tap the automation's run/play control if iOS shows one). A successful call returns JSON like `{"ok": true, "person_id": "ana", "state": "away"}`; the Security → Presence card then shows that person as `Shortcut · Person`. Opening the URL in Safari is not a valid test because Safari sends `GET` and the endpoint intentionally accepts only `POST`.

The browser-only **This device** row is diagnostic: it uses the browser Geolocation API and only updates while the dashboard tab/PWA is open. It is useful for setting/checking the home location, but it does not drive alarm automation. Find My/iCloud entries are also diagnostic enrichment; the reliable automation source is the Shortcut webhook state persisted in `config/presence_state.json`.

Home location is editable in the Presence card. The **Use this device location** button asks the browser for the current GPS position and writes it to `config/location.json`; the latitude/longitude fields can also be typed manually. After saving, Find My diagnostics can be refreshed from the Presence card so distances recalculate from the new origin.

Alarm behavior:

- Everyone visible/confirmed away for the configured grace period → `control_system("arm")`.
- First fresh confirmed arrival while armed → `control_system("disarm")`.
- Stale/uncertain state never disarms.
- Any manual alarm action in the UI suppresses automation until a later presence transition.
- Each attempted transition appends a JSONL audit row to `logs/presence_triggers.jsonl` (gitignored).

### Web Push setup

Web Push is browser-native push for the installed PWA. The app stores a browser subscription locally and uses VAPID keys to send a notification from the server when a presence transition fires. There is no third-party notification account, but iOS requires the dashboard to be opened as an installed PWA from the home screen before push subscriptions work.

Generate local VAPID keys:

```powershell
& .\.venv\Scripts\python.exe scripts\gen_web_push_keys.py mailto:you@example.com
```

Restart the webapp, open the installed PWA over HTTPS, go to Security → Presence, and tap **Enable notifications**. The private key and subscriptions live in gitignored `config/push_config.json` and `config/push_subscriptions.json`. Push delivery is best-effort; a failed notification never blocks arm/disarm.

## Home-network / Network tab

Issue #125 added a read-only spike for the **Network** view; issue #129 builds the tab itself, in phases. **Phase 1 (live now):** the **📶 Network** tab sits between Plugs and Security and shows internet/WiFi/LAN health, the attached-device inventory grouped by band (weakest signal first), network-quality alerts, AP/router health, and the confirm-gated **Reboot AP** — so the network can be watched and managed without logging into the vendor web UIs by hand. The router-reboot button is present but disabled until Phase 3 wires the router data-read. **Phase 2 (live now):** device identity + rename — each device row shows a category glyph and a friendly label (custom name → OUI vendor → reported hostname → MAC), and tapping it opens a detail modal (vendor, IP, band, signal, SSID, MAC) with a rename field. Vendor comes from a bundled, trimmed OUI table (offline, slightly stale — an unknown prefix just falls back to the hostname/MAC); modern phones rotate a randomised MAC per network, so those are flagged and left un-vendored. **Phase 3 (live now):** the router card fills in with the live **WAN/internet status** (WAN up/down, public IP, default gateway, DNS, connection name, link uptime) read from the ZTE web API, and the **Reboot** button is enabled (confirm-gated, ~5-min outage). **Phase 4 (live now):** device **history & smart alerts** — a per-MAC registry (`network_history.py`) remembers each device, so the list shows **online/offline** state with "last seen Xh ago" on absent devices, a **Show offline** toggle (persisted, only shown when there are offline devices), a **new** badge for devices first seen in the last 24 h, and a **Mark important** switch in the detail modal. Two derived alerts join the strip: a never-before-seen device joining, and an *important* device dropping offline. **Network cleanup (live now):** attached devices also have a persisted **Hidden** switch in the detail modal; hidden devices stay out of the default list, show as dimmed when the header **Show hidden** pill is active, and remain separate from delete/history pruning. **Wi-Fi diagnostics (live now):** a collapsible tile reads the dashboard PC's own Windows Wi-Fi interface (`netsh wlan`) and shows the current SSID/signal/channel plus visible BSSIDs, signal %, band/channel, emitter MAC, and separate 2.4/5 GHz channel-overlap charts in the same translucent line/area style as the solar charts. Tapping a Wi-Fi row opens a detail modal with custom label, original SSID/BSSID, security/channel/signal, and a persisted **Hidden** switch; labels and chart tooltips use the custom name while preserving raw identity in the detail view. Randomised MACs aren't tracked (a rotating address isn't a stable device, and tracking it would spam the new-device alert). Full findings and the follow-up checklist are in [`docs/network-spike.md`](docs/network-spike.md).

- **Endpoints:** `GET /api/network` (one snapshot — `internet` / `access_point` / `router` / `wifi` / `devices` / `alerts`; the `wifi` block is best-effort and carries `available`, current interface/SSID/BSSID/signal/channel/band, visible BSSIDs, custom `display_name`, raw `original_name`, stable `wifi_id`, `hidden`, read-only recommendations, and structured `insights` with channel scores/rationale for later channel-management automation; the `router` block carries `wan_online` / `public_ip` / `gateway` / `dns` / `connection_name` / `uptime_s`; per device the merged `display_name`, `hidden`, OUI `vendor`, `category`, `randomized` flag, plus the Phase-4 history fields `online` / `first_seen` / `last_seen` / `times_seen` / `important` / `is_new`, and synthesised `online:false` rows for known-but-absent devices; an unreachable AP, router, or Wi-Fi scan is reported on its card/block, not an error), `GET /api/network?speedtest=1` (adds an opt-in `speedtest-cli` throughput run, ~13 s — never auto-run), `POST /api/network/access-point/reboot` (reboots the R9000; styled confirm, all clients drop ~1–2 min), `POST /api/network/router/reboot` (reboots the F6600P; styled confirm, ~5-min full outage), `PUT /api/network/devices/{mac}/display_name` (set/clear a device's custom label, persisted to gitignored `config/network_display_names.json` keyed by normalised MAC), `PUT /api/network/devices/{mac}/hidden` (hide/restore an attached device, persisted to gitignored `config/network_hidden.json`), `POST /api/network/devices/{mac}/important` (mark/unmark a device important — the flag lives in the history registry and an important device is never auto-pruned), `PUT /api/network/wifi/display_name` (set/clear a Wi-Fi label, persisted to gitignored `config/network_wifi_display_names.json` keyed by BSSID or explicit `SSID:<name>` fallback), and `PUT /api/network/wifi/hidden` (hide/restore a Wi-Fi row, persisted to gitignored `config/network_wifi_hidden.json`).
- **Cadence:** the tab refreshes every ~15 s **only while it is open** (the AP SOAP read + router WAN read are comparatively expensive) and stops polling when you leave it; the speed test is an explicit button, never on the poll.

It reads three independent sources: the **NETGEAR R9000 access point** over the Netgear SOAP API (`pynetgear`) for the device inventory + AP health + reboot; the **Vodafone ZXHN F6600P router** (ZTE) over its SHA256 web login, with authenticated WAN-status reads and reboot riding the per-request session-token + RSA integrity (`Check`) scheme its web UI uses; and **internet health host-side** (OS ping latency/packet-loss + an optional `speedtest-cli` throughput run). The AP runs in access-point mode but still reports the whole wired+wireless LAN, so it carries the inventory on its own.

Config in `.env`:

| Key | Meaning |
|-----|---------|
| `NETWORK_AP_HOST` | Access-point LAN IP. |
| `NETWORK_AP_USERNAME` | AP web-admin user (usually `admin`). |
| `NETWORK_AP_PASSWORD` | AP web-admin password. |
| `NETWORK_ROUTER_HOST` | Router (gateway) LAN IP. |
| `NETWORK_ROUTER_USERNAME` | Router web user (usually `user`). |
| `NETWORK_ROUTER_PASSWORD` | Router web password. |

Run the smoke command:

```powershell
& .\.venv\Scripts\python.exe -m src.list_network              # health + inventory
& .\.venv\Scripts\python.exe -m src.list_network --speedtest  # + WAN throughput (~15 s)
& .\.venv\Scripts\python.exe -m src.list_network --reboot-ap   # reboot the AP (drops WiFi ~1-2 min)
```

The CLI prints internet health (up/down, latency, packet loss, optional speed), AP and router health, host-PC Wi-Fi diagnostics, and the attached-device list (MAC, IP, band, signal %, name) with weak-signal/offline alerts. Do not commit device credentials, the WiFi SSID/password, visible SSID/BSSID scan dumps, LAN IPs, or MAC/device dumps; this repository is public.

## SMA solar / energy

The dashboard shows the home's live energy flow (☀️ Solar · 🏠 House · ⚡ Grid ·
♻️ Net) as the read-side foundation of the eventual solar load-balancing
automation (shift HVAC load to match PV). When `SMA_CLOUD_PLANT_ID` is set, it
uses the same Sunny Portal energy-balance values shown in the SMA Energy app.
If cloud is not configured, unavailable, or **stale** (the widget keeps echoing
its last point after the Sunny Home Manager stops uploading — see
`SMA_CLOUD_MAX_STALENESS_S`), it falls back to local LAN reads:

- **Sunny Home Manager 2.0 / energy meter** — read over **Speedwire** (UDP
  multicast) with **no credentials**. Gives grid import/export + cumulative
  counters. Discovered automatically on the LAN.
- **PV inverter** (Tripower X / ennexOS) — read over its **local ennexOS web
  API**, logging in with the SMA account. Gives PV production. SMA inverters
  **power down at night**, so the inverter only appears on the network while
  producing; an asleep inverter is reported as such (PV unknown), not an error.

**When live data is unavailable, the app says why (issue #101).** If the energy
meter stops answering on the LAN (cloud stale *and* no Speedwire response), the
live-flow tile shows an inline `Live unavailable — the energy meter is not
responding on the LAN` note instead of bare `—`; the cumulative/history cards
keep rendering from their own sources. More broadly, a hard data-fetch failure on
the Energy, Plugs, or Security tab now raises a single error **toast** naming the
source and reason — surfaced once per outage (the tabs poll every few seconds, so
it does not repeat while a source stays down) and re-armed on recovery.

Config in `.env`:

| Key | Meaning |
|-----|---------|
| `SMA_CLOUD_PLANT_ID` | Sunny Portal plant/component ID. When set, the app reads the same cloud energy-balance values shown in the SMA Energy app. |
| `SMA_CLOUD_MAX_STALENESS_S` | Max age (seconds, default `900`) of a cloud energy-balance point before it is treated as stale and the read falls through to the live local sources — stops a frozen cloud value from flat-lining the live chart. |
| `SMA_INVERTER_HOST` | Inverter LAN IP/host. Blank → read the meter only. |
| `SMA_INVERTER_ACCESS_METHOD` | `ennexos` (default) or `speedwireinvV2` for Speedwire-only inverters. |
| `SMA_INVERTER_GROUP` | Speedwire login group: `user` (default) or `installer`. |
| `SMA_INVERTER_PASSWORD` | Local Speedwire inverter password (max 12 chars). Use this instead of the SMA cloud password for Speedwire devices. |
| `SMA_USER` / `SMA_PASSWORD` | SMA account, for Sunny Portal cloud login and ennexOS local login. |

Find the inverter IP by running the CLI **in daylight** (it is off-network at
night):

```powershell
& .\.venv\Scripts\python.exe -m src.list_energy      # Windows
```

```bash
./.venv/bin/python -m src.list_energy                # POSIX
```

then set `SMA_INVERTER_HOST` to the address it logs and restart the tray.
`GET /api/energy` serves the same snapshot to the PWA.

## Energy monitoring & history

The PWA splits into seven tabs: **Home** (a consolidated dashboard — weather strip,
the actionable alarm tile, a one-line-per-unit AC summary with inline power
toggles, a plug summary, and the same live ☀️ Solar · 🏠 Home · 🗼 Grid energy-flow
card as the Energy tab; alarm + AC act, the rest inform),
**AC** (the full unit controls + detail modal),
**Energy** (an SMA-style solar dashboard — a live ☀️ Solar · 🏠 Home · 🗼 Grid
flow row with a colour-coded grid arrow (blue ◀ importing, green ▶ exporting),
self-sufficiency / self-consumption tiles, today's generation & consumption split
cards, a savings estimate (€ saved on self-consumed PV at the configured tiered
rate, plus CO₂ avoided + trees), an all-positive Generation/Grid-supplied/Consumption
live chart, a Day/Week/Month/Year/Σ history chart, and a **cost & savings
breakdown** table (grid energy priced per time-of-use period, self-consumed PV
valued at the avoided rate — see *Electricity tariff* below), **🔌 Plugs** (the local Smart Life
devices — see below), **💡 Light** (Elgato lights — see below), **📶 Net** (LAN health, the attached-device inventory, and the AP reboot —
see below), and **🛡️ Alarm** (RISCO alarm controls, event log, and
detector bypass).

On desktop the tabs are a top segmented control; on a phone / installed PWA they
become a floating bottom tab bar with stroke icons (mirroring the `app-launcher`
nav). Collapsible sections (Settings, Security's event log + detectors) share one
centered, icon-led summary style.

While the webapp runs, a background **sampler** (started in the FastAPI
lifespan, so it lives and dies with the tray's uvicorn process) persists the
live energy flow to a server-side SQLite database. Recent raw samples feed the
live chart; completed hours are folded into compact rollups that daily and
monthly views group from. An **asleep inverter is stored as no PV reading**, not
a misleading 0, so the charts show a gap and aggregates flag `pv_missing`.

- **Storage:** `webapp/energy_history.sqlite3` (gitignored, per-machine runtime
  data — never committed).
- **Endpoints:** `GET /api/energy` (live snapshot), `GET /api/energy/today`
  (today's totals for the split + savings cards), `GET /api/energy/history?minutes=N`
  (raw samples for the live chart), `GET /api/energy/aggregate?range=day|week|month|year|total`
  (energy-per-bucket, Wh — `day` is a 24h fill-up frame; `week`/`month`/`year` are
  rolling data-only windows; `total` is all retained history), and
  `GET /api/energy/cost?range=day|week|month|year|total` (tiered cost & savings
  breakdown over the same windows — per-period consumption/grid/solar/cost/savings
  plus a fixed-cost + estimated-bill summary; see *Electricity tariff* below).
- **Cadence/retention knobs** (`.env`, all optional):

| Key | Default | Meaning |
|-----|---------|---------|
| `ENERGY_SAMPLER_ENABLED` | `true` | Master switch. `false`/`0` serves live + existing history but persists nothing (used by the e2e suite and dev runs so they don't poll SMA). |
| `ENERGY_PERSIST_INTERVAL_S` | `60` | Seconds between persisted samples. For a 5-minute cloud source (Sunny Portal) the data simply won't change faster than the source. |
| `ENERGY_COMPACT_INTERVAL_S` | `3600` | How often completed hours are folded into rollups and old raw data pruned. |
| `ENERGY_RAW_RETENTION_DAYS` | `7` | How long raw per-sample rows are kept (feeds the live chart). |
| `ENERGY_HOURLY_RETENTION_DAYS` | `400` | How long hourly rollups are kept (daily/monthly views group from these). |

The live display refreshes every ~5 s while the Energy tab is open and every
30 s otherwise; that cadence is a frontend constant (it never persists faster
than `ENERGY_PERSIST_INTERVAL_S`). Energy (Wh) is integrated from the samples
(rectangular rule, gaps capped) — a household-monitoring estimate, not a
billing-grade meter read.

### Electricity tariff (cost & savings)

The Energy tab's **cost & savings breakdown** prices grid energy per time-of-use
period and values the self-consumed PV at the same avoided rate. Rates come from
a per-machine tariff file:

- **Config:** `config/tariff.json` (gitignored) — copy `config/tariff.sample.json`
  and fill in your rates from your electricity invoice. Each period's
  `price_eur_kwh` is **pre-tax** (energy commodity + access tolls + system
  charges); the app adds `electricity_tax_eur_kwh` and `vat_pct` on top, so the
  all-in price of a kWh is `(price + electricity_tax) × (1 + vat_pct/100)`.
- **Calendar:** `"2.0TD"` applies the Spanish 2.0TD time-of-use bands (P1 punta,
  P2 llano, P3 valle, weekends/holidays all-valle); any other value treats the
  first period as a single flat rate. Optional `holidays` (a list of
  `"YYYY-MM-DD"`) are billed as valle.
- **Fallback:** with no `config/tariff.json`, the breakdown still renders at a
  flat **€0.10/kWh** estimate (`configured: false`) — clearly labelled in the UI.
- **What it computes:** per-period consumption / grid-import / solar-covered kWh,
  grid cost €, and savings € (avoided cost of self-consumed PV), plus a summary
  with the prorated fixed standing charge, an estimated bill, and the "without
  solar" cost. Export is credited at `export_eur_kwh` (0 = no feed-in payment).

How the period prices and the model are derived from a real PVPC 2.0TD invoice —
including the PVPC hourly-market approximation and the bono-social handling — is
documented in [`docs/tariff-model.md`](docs/tariff-model.md).

> **Data-retention caveat.** The breakdown is computed from the local history DB,
> so the **Year** and **Σ Total** windows only fill in as the sampler accrues
> data (hourly rollups are kept `ENERGY_HOURLY_RETENTION_DAYS`, default ~400
> days). A freshly-started instance shows mostly empty long windows until history
> builds up — this is an estimate from monitored data, not your utility's meter.

## Weather

The **Home tab** shows a compact weather strip — current weather (icon +
temperature) and today's forecast (min / max + a forecast icon) on the left, with
a small transparent light/dark **theme toggle** on the right — for the home
location, read from **Open-Meteo** (keyless — no account, no API key). The clock
was dropped (it duplicated the phone's status bar) and the `label` is **not**
rendered (it's obviously home), so the strip stays on a single line. Settings
(incl. the other theme toggle) lives on the non-Home tabs to keep Home clean.

- **Location config:** the home coordinates live in `config/location.json`
  (`lat` / `lon` / optional `label`). This file is **gitignored** — the repo is
  public, so the home location never enters git. Copy `config/location.sample.json`
  (placeholder `0.0/0.0`) to `config/location.json` and fill in your own lat/lon
  (geocode an address with any keyless geocoder, e.g.
  `https://geocoding-api.open-meteo.com/v1/search?name=<city>`).
- **Endpoint:** `GET /api/weather` returns `{available, temperature_c,
  weather_code, is_day, label, temp_min_c, temp_max_c, forecast_code}` (the last
  three are today's forecast, from Open-Meteo's `daily` block; null if that block
  is missing). When `config/location.json` is absent or Open-Meteo is unreachable
  it returns `{available: false, reason}` with HTTP 200 — weather is decorative,
  never a 500. The tile stays hidden until the first successful read and fails
  quietly thereafter. The clock ticks client-side, independent of the poll.
- **Cadence:** the frontend polls every ~10 minutes (weather barely moves).

## Solar forecast (expected generation)

The Energy tab's **Solar forecast** card shows an *expected generation* curve
(dashed) with the day's measured generation overlaid (filled), a headline
"Expected generation +X kWh", a caption with the array parameters the curve was
computed from (e.g. `1.5 kWp · 35° tilt · S · PR 0.80`), and a **Yesterday /
Today / Tomorrow** toggle. It is read/visualisation only — a forecast to compare
against reality, not a control input.

- **Source:** one keyless **Open-Meteo** call for hourly *global tilted
  irradiance* (the same host the weather tile uses), scaled by the array to an
  expected-generation curve. Self-contained and approximate — see
  [`docs/pv-forecast.md`](docs/pv-forecast.md) for the model.
- **Config:** `config/pv_system.json` (gitignored) — copy
  `config/pv_system.sample.json` and set `kwp`, `tilt_deg`, `azimuth_deg`
  (Open-Meteo convention: 0 = South, −90 = East, 90 = West), and
  `performance_ratio`. Coordinates are reused from `config/location.json` (the
  weather tile's file) — there is no separate lat/lon.
- **Endpoint:** `GET /api/energy/forecast?day=yesterday|today|tomorrow` returns
  the hourly expected curve, the day's `expected_total_kwh`, the `system` params
  used (kWp / tilt / azimuth / performance_ratio), and (for today/yesterday) the
  measured `actual` overlay (`null` for tomorrow). When
  `pv_system.json`/`location.json` is absent or Open-Meteo is unreachable it
  returns `{available: false, reason}` with HTTP 200 — the card keeps a one-line
  note and nothing else breaks.

## Tuya / Smart Life

Smart Life devices are Tuya devices. This project uses `tinytuya` as a local LAN control foundation. Runtime reads and commands use the local keys stored in gitignored `devices.json`; they do not require an active Tuya Cloud project once that file exists.

One-time Tuya bootstrap, only needed when `devices.json` must be generated or refreshed from the Smart Life account:

1. Register or log in at `https://iot.tuya.com/`.
2. Create a **Smart Home** Cloud Project in the **Central Europe** data center.
3. Link the Smart Life mobile app account to that project by scanning the QR code from the Tuya developer portal.
4. Copy the project's **Access ID / Client ID** and **Access Secret / Client Secret** into `.env` for the wizard:

| Key | Meaning |
|-----|---------|
| `TUYA_API_KEY` | Tuya Cloud Project Access ID / Client ID, used only for TinyTuya bootstrap. |
| `TUYA_API_SECRET` | Tuya Cloud Project Access Secret / Client Secret, used only for TinyTuya bootstrap. |
| `TUYA_REGION` | Tuya data-center region for bootstrap. Use `eu` for Central Europe. |

Then fetch the device list and local keys:

```powershell
& .\.venv\Scripts\python.exe -m tinytuya wizard
```

The wizard writes `devices.json` in the project root. That file contains device IDs, local keys, IPs, protocol versions, and DPS mappings; it is required for LAN-mode control and is gitignored because it contains secrets. TinyTuya may also write `tinytuya.json`; that is gitignored as well. Energy DPS varies by plug model, so `src/tuya_client.py` reads the captured `mapping` block instead of assuming fixed DPS indexes.

### 🔌 Plugs tab

The PWA's **Plugs** tab is a Smart-Life-style control surface for these local Tuya devices: a dense mobile grid showing every captured device at once, with each tile a single **name · wattage · on/off** row (**live wattage on metered plugs**, so solar/load decisions are obvious without opening the vendor app), and open/stop/close controls for covers. A **summary block** at the top totals devices, switches on, switches off, and live consumption (summed across reachable metered plugs). It is **cloud-free at runtime** — it reads `devices.json` plus local LAN status only.

- **Rename a socket:** tap a plug's name to open a rename modal (same UX as the AC-unit rename). The custom label is saved via `PUT /api/tuya/{id}/display_name` to a gitignored `config/tuya_display_names.json` (`device_id` → label, parallel to the unit `config/display_names.json`); a missing file is not an error. The override wins over the Tuya device name everywhere in the UI.
- **Endpoints:** `GET /api/tuya` (device cards with switch state, reachability, live energy fields, and the `display_name` override — the per-device LAN reads run in parallel), `POST /api/tuya/{id}/switch` (`{"on": true|false}`), `POST /api/tuya/{id}/cover` (`{"action": "open"|"close"|"stop"}`), `PUT /api/tuya/{id}/display_name` (`{"display_name": "…"}`; empty clears the override).
- **Cadence:** the tab refreshes every ~15 s **only while it is open** (LAN reads are comparatively expensive), and stops polling when you leave it.
- **Offline devices:** a powered-off plug or one without a usable LAN IP renders as **Unavailable** without blocking the reachable devices from updating or being controlled.
- **Missing or stale devices?** Re-run the TinyTuya wizard/snapshot **on the home network** to refresh `devices.json` (new IPs, new devices, updated local keys) — never the cloud:

  ```powershell
  & .\.venv\Scripts\python.exe -m tinytuya wizard      # full re-pair (new devices / keys)
  & .\.venv\Scripts\python.exe -m tinytuya snapshot    # quick IP/state refresh of known devices
  ```

## Elgato lights

The **Light** tab controls Elgato Key Light style devices directly over the
local LAN HTTP API. It is cloud-free at runtime: the backend tries Bonjour/mDNS
discovery for `_elg._tcp.local.` and also supports an explicit host fallback
for networks where discovery is blocked. It has per-light power, brightness,
and warmth controls, exact numeric entry beside each slider, state-aware
all-on/all-off buttons for reachable lights, and a detail modal that saves a
custom label in gitignored `config/elgato_display_names.json` while showing the
original Elgato identity, LAN address, MAC metadata when available, firmware,
and colour-temperature readback. Spike findings and the implementation choice
are recorded in [`docs/elgato-lights.md`](docs/elgato-lights.md).

Optional config in `.env`:

| Key | Meaning |
|-----|---------|
| `ELGATO_LIGHT_HOSTS` | Optional comma-separated `host[:port]` list. Leave blank to try mDNS discovery only. The default port is `9123`. |

Endpoints:

- `GET /api/lights` — list Elgato lights with reachability, display-name
  override, original name, product, firmware, host/port, optional MAC metadata,
  power, brightness, and color temperature.
- `POST /api/lights/{id}` — set `{"on": true|false}`,
  `{"brightness": 3..100}`, `{"temperature": 143..344}`, or
  `{"temperature_k": 2900..7000}`; the response is the live read-back.
- `PUT /api/lights/{id}/display_name` — save or clear the local label override.

Smoke command:

```powershell
& .\.venv\Scripts\python.exe -m src.list_elgato_lights
& .\.venv\Scripts\python.exe -m src.list_elgato_lights --id 192.168.0.50:9123 --on on --brightness 40 --kelvin 4000
```

Do not commit real light hostnames/IPs, room names, or screenshots containing
private room names; this repository is public. If discovery finds nothing but
the Elgato phone app works, set `ELGATO_LIGHT_HOSTS` to the light's LAN IP and
restart the webapp.

## Cameras

The **Security** tab has a collapsible **📹 Cameras** tile for open
RTSP/ONVIF cameras (a **Reolink E1 Outdoor Pro** today) — no vendor cloud, hub,
or subscription (path 2 from the #85 feasibility study; the #89 spike findings
and go/no-go are in [`docs/camera-spike.md`](docs/camera-spike.md)). Each camera
is a row showing its model and reachability; tapping it opens a detail modal with
a **fresh snapshot** (grabbed at open time) and a rename field, and an **Open live
view** button. The full-screen live view streams MJPEG, with a **PTZ d-pad**
(pan/tilt + zoom, press-and-hold), a **screenshot** button (downloads a still),
and a **record** toggle (server-side mp4). Cameras are accessed the same
vendor-neutral way the rest of the fleet is: ONVIF for discovery/profiles/PTZ,
RTSP + **ffmpeg** for the snapshot/stream/recording. The eventual goal is
alarm-triggered scene capture with AI analysis.

- **Config:** declare cameras in gitignored `config/cameras.json` — copy
  `config/cameras.sample.json` and fill in each camera's `id`, `host`,
  `onvif_port` (Reolink default 8000), `rtsp_port` (554), `username`, and
  `password` (the on-device **device account**, NOT the cloud login). Custom
  labels persist to gitignored `config/camera_display_names.json`.
- **Prerequisite:** **enable RTSP + ONVIF on the camera first** — Reolink ships
  them off (app: Settings → Network → Advanced → Server Settings).
- **Endpoints:** `GET /api/cameras` (list), `GET /api/cameras/{id}/snapshot`
  (fresh JPEG), `GET /api/cameras/{id}/stream` (live MJPEG; reachable from the PWA
  via `?token=`), `POST /api/cameras/{id}/ptz` (`{action: start|stop, direction,
  zoom}`), `POST /api/cameras/{id}/record` (`{action: start|stop}` → mp4 in
  gitignored `webapp/camera_captures/`), `PUT /api/cameras/{id}/display_name`.
- **Needs ffmpeg on PATH.** Smoke command:

  ```powershell
  & .\.venv\Scripts\python.exe -m src.list_cameras
  ```

Do not commit camera IPs, the device-account password, the UID/MAC, captured
frames, or location names; this repository is public.

## Run the webapp (the product)

### Via the tray (the always-on way)

```powershell
.\tray.bat                                                        # Windows
```

`tray.bat` puts a **system-tray icon** in the notification area that owns the
webapp's lifecycle — it spawns and supervises the uvicorn server, so the
dashboard is up from login without a console window. Drop a shortcut to
`tray.bat` in the **Startup folder** (`shell:startup`) for always-on use.

- Idempotent: a second `tray.bat` no-ops if a tray is already running.
- `tray.bat --restart` stops the running tray, **reclaims `:8447` even from an
  orphaned uvicorn**, and starts a fresh one — this is how a new pull is
  picked up (run it after editing `src/` or `app/`).
- Tray menu: **Open** the dashboard, **Copy local/Tailscale URL** (token
  appended), **Restart webapp**, **Status**, **Quit** (stops the webapp
  cleanly — no orphaned process on `:8447`). **Copy Tailscale URL** copies the
  full tailnet FQDN (`https://<pc>.<tailnet>.ts.net:8447?token=…`) — the only
  form that resolves over MagicDNS from a phone — falling back to the `100.x`
  tailnet IP; both are covered by the cert SANs.

The tray launches `python -m app.tray`; detection/kill is scoped to *this*
repo's `.venv` by command line, so sister-app trays are never touched.

### Headless / dev (no tray)

```powershell
.\webapp.bat                                                      # Windows
```

`webapp.bat` binds `0.0.0.0:8447` and serves **HTTPS** when
`webapp/certificates/cert.pem` exists (see [HTTPS](#https-self-signed-ca)),
otherwise plain HTTP. Invoke uvicorn directly if you prefer:

```powershell
& .\.venv\Scripts\python.exe -m uvicorn app.webapp.server:app --host 0.0.0.0 --port 8447 `
    --ssl-keyfile webapp/certificates/key.pem --ssl-certfile webapp/certificates/cert.pem
```

The signal that new code is live is the unit grid rendering (6 units).

## HTTPS (self-signed CA)

Generate a local CA + leaf cert. The leaf's SANs auto-include loopback, the
machine hostname, LAN IPs, and — when Tailscale is installed — the tailnet
MagicDNS name + `100.x` address, so the same cert is trusted over LAN and
Tailscale alike:

```powershell
& .\.venv\Scripts\python.exe scripts\gen_ssl_cert.py
```

On Windows this also installs the CA into `CurrentUser\Root` so Edge/Chrome
on the PC trust it (use `--skip-install` to skip).

> **TLS renewal — regenerate before ~July 2027.** The leaf cert is capped at
> 396 days because Apple/WebKit reject server certs valid > 398 days. After
> ~13 months Safari shows "Not Secure" again — that's the leaf expiring, not
> a regression. Re-run `gen_ssl_cert.py` (it reuses the existing CA, so no
> device re-trust is needed) and restart the webapp.

### Phone install (PWA)

The webapp installs to the iPhone/Android home screen as a full-screen app.
Because the cert is self-signed, first-time iOS setup is a short detour:

1. In the dashboard, expand **⚙️ Settings** → tap **📲 Install certificate** (or open `https://<pc-hostname>:8447/install-ca` directly) in Safari → **Allow** to download the profile.
2. **Settings → General → VPN & Device Management** → tap the profile → **Install**.
3. **Settings → General → About → Certificate Trust Settings** → toggle the CA **ON**.
4. Force-quit Safari, reopen the URL (the lock icon should be solid), then **Share → Add to Home Screen**.

On Android, Chrome offers "Install app"; the CA is also served as DER at `/static/ca.crt`.

## Auth (token + password)

Both layers are optional. With nothing configured the API is open (fine on a
trusted tailnet). Loopback callers always bypass the gate.

The PWA keeps a browser-local, versioned last-good snapshot of selected read-only API responses (AC units, live/today energy, plugs, lights, and network) so a reload can paint the last useful state before fresh live reads finish. Successful live reads replace the snapshot; failed reads leave the last-good snapshot intact. Security state, presence/location diagnostics, event logs, auth responses, edit forms, and command responses are intentionally excluded.

```powershell
& .\.venv\Scripts\python.exe scripts\gen_token.py            # set a bearer token
& .\.venv\Scripts\python.exe scripts\gen_token.py --force    # rotate
& .\.venv\Scripts\python.exe scripts\gen_token.py --clear    # disable
& .\.venv\Scripts\python.exe scripts\set_password.py <pw>    # optional login password
```

- Remote (LAN / Tailscale) callers must present `Authorization: Bearer <token>` or `?token=…`.
- Open the webapp once with `?token=…`; the page stashes it in localStorage and strips it from the URL.
- A login **password** lets a fresh device (e.g. an iOS PWA whose storage is partitioned) type a secret into the overlay instead — the server hands the token back. Failed attempts log to `webapp/auth.log`.

## Custom unit names

Each HVAC unit has a factory name supplied by MELCloud (e.g. "MSZAP15VGK").
You can override it with a friendlier label — "Living Room", "Master Bedroom" —
that is shown in the card grid and the detail modal instead of the API name.

- Open the detail modal for a unit → fill in the **Display name** field → the
  label is saved immediately via `PUT /api/units/{id}/display_name` and
  reflected on the card without a page reload.
- Overrides are stored in `config/display_names.json` (gitignored — it would
  expose room names in a public repo). A template with the JSON structure is at
  `config/display_names.sample.json`:

  ```json
  {
    "12345": "Living Room",
    "67890": "Master Bedroom"
  }
  ```

  Keys are unit IDs (strings); values are the display names. The file is
  optional — a missing file is silently treated as no overrides.

## HVAC automation

The unit detail modal has two optional automation sections:

- **Temperature rule** — a dynamic setpoint controller, not an on/off thermostat.
  The unit stays on only if you turned it on; while it is on in Cool/Dry or Heat,
  the webapp steers the unit setpoint every 15 minutes (default) to keep the
  measured room temperature near the configured room target, with a 0.5 °C
  buffer. The loop is asymmetric: while the room is still past the target it
  nudges one step at a time, but the moment the room reaches the target it jumps
  the setpoint to one degree on the satisfied side (Cool: target + 1; Heat:
  target − 1) so the unit idles immediately instead of overshooting deep and
  recovering one step at a time. Auto/Fan modes are not steered.
- **Schedules** — multiple daily `HH:MM` entries per unit. An entry can be a
  simple off event, or an on event that applies a full profile (mode, target
  temperature, fan, and vanes) at that time.

Rules and schedules are evaluated server-side by the tray-owned webapp, so they
work while the PWA is closed. Runtime files live in gitignored
`config/hvac_rules.json` and `config/hvac_schedules.json`; committed samples show
the shape. Existing single-schedule config files are loaded as one-entry lists on
upgrade. Optional `.env` knobs:

| Key | Default | Meaning |
|-----|---------|---------|
| `HVAC_AUTOMATION_ENABLED` | `true` | Master switch. Set `false`/`0` to disable the evaluator. |
| `HVAC_POLL_INTERVAL_S` | `60` | How often the evaluator checks configured rules/schedules. With no config it does not hit MELCloud. |
| `HVAC_ADJUST_INTERVAL_S` | `900` | Minimum time between dynamic setpoint nudges per unit. |
| `HVAC_BUFFER_C` | `0.5` | Room-temperature hold band around the rule target. |

## CLI

Print every device's live state:

```powershell
& .\.venv\Scripts\python.exe -m src.list_devices                  # Windows
```

```bash
./.venv/bin/python -m src.list_devices                            # POSIX
```

Print visible iCloud Find My presence entities:

```powershell
& .\.venv\Scripts\python.exe -m src.list_presence                 # Windows
```

```bash
./.venv/bin/python -m src.list_presence                           # POSIX
```

## Tests

Two suites, both run with the same interpreter and `pytest`.

### Backend suite — fast, no network or browser

A Python-level layer under `tests/` (excluding `tests/e2e/`) exercises the real
backend in-process:

- **API smoke** (`tests/api/`) drives `app.webapp.server:app` with FastAPI's
  `TestClient` (presenting as a loopback caller, so the bearer gate is bypassed
  exactly as it is for local probes). It asserts `/healthz`, `/api/version`, and
  `/` answer, and that `/api/units` + `/api/energy` flatten their fetched data —
  with the cloud fetchers **monkeypatched**, so it never calls
  MELCloud Home / SMA / Tuya / Risco.
- **Unit tests** cover the pure logic: `src.tariff` (tiered-rate cost/savings +
  the 2.0TD calendar), `src.energy_history` (record → aggregate round-trip and
  bucketing against a `tmp_path` SQLite DB with a fixed `now=`), and
  `src.display_names` (atomic set/load/clear round-trip).

```powershell
& .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
& .\.venv\Scripts\python.exe -m pytest tests -p no:cacheprovider --ignore=tests/e2e
```

### Browser E2E suite

A Playwright browser-E2E suite lives in `tests/e2e/`. It boots the real
webapp (adopting a running one on :8447, else autobooting a disposable
instance with the energy sampler off) and drives the PWA, **stubbing
`/api/units`, the `/api/energy*` endpoints, and `/api/tuya*` with fixtures** so
it never touches the live cloud, the LAN, or actuates real HVAC. Coverage
includes the Home/AC/Energy/Plugs tab navigation, the Home AC summary, an
Energy-tab render (hero numbers + charts), and the Plugs tab (metered-plug
watts, a switch round-trip, cover controls, and an offline device). Runs in two
projections — Chromium desktop + WebKit on an iPhone 14.

```powershell
& .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
& .\.venv\Scripts\python.exe -m playwright install chromium webkit
& .\.venv\Scripts\python.exe -m pytest tests/e2e                       # both projections
& .\.venv\Scripts\python.exe -m pytest tests/e2e --browser chromium    # faster dev loop
```

## Streamlit spike (POC)

A lightweight, **throwaway** data/debug view — independent from the product,
sharing only `src/melcloud_client.py`. Not the real UI; kept only as a fast
way to eyeball the data.

```powershell
.\launch_app.bat                                                  # Windows (http://localhost:8501)
```

```bash
./launch_app.sh                                                   # POSIX
```

…or directly: `& .\.venv\Scripts\python.exe -m streamlit run spike/streamlit_app.py`.
