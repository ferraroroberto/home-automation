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
  - `risco_client.py` — async RISCO Cloud alarm state (incl. system-wide low-battery + per-zone trouble flags), controls, event log, and detector bypass.
  - `webapp_config.py` — webapp host/port + auth secrets loader.
  - `static_versioning.py` — build identity (git SHA) + content-hash (`?v=`) stamping of the PWA's `.js`/`.css` URLs so a mobile PWA never serves stale cached code.
- **`app/webapp/`** — the FastAPI + PWA product.
  - `server.py` — `create_app()`, middleware, caching static mount, routers, background-task lifespan.
  - `middleware.py` — bearer-token / loopback auth gate.
  - `manager.py` — adopt-or-spawn / restart / stop for the uvicorn webapp (used by the tray).
  - `sampler.py` — background energy sampler owned by the webapp lifecycle.
  - `automation.py` — background HVAC automation evaluator (dynamic setpoint rules + schedules) owned by the webapp lifecycle.
  - `routers/` — `units` (read + control), `energy` (live flow + history/aggregate + cost breakdown), `tuya` (local Smart Life devices + watts), `security` (RISCO alarm state/control), `auth` (login), `misc` (page, health, CA profile).
  - `static/` — the PWA (HTML/CSS/ES-modules), `manifest.webmanifest`, icons.
    Modules: `main.js` (boot + AC cards), `tabs.js` (Home/AC/Energy/Plugs switcher),
    `energy.js` (energy tab + live polling), `plugs.js` (Smart Life tab),
    `security.js` (RISCO alarm tab),
    `charts.js` (Chart.js wrappers), `state.js`, `api.js`;
    `vendor/chart.umd.min.js` (vendored Chart.js v4).
- **`app/tray/`** — the Windows tray that owns the webapp lifecycle (`tray.bat`).
  - `tray.py` — pystray icon + menu; `__main__.py` — the `-m app.tray` entry.
  - `single_instance.py`, `tray_lifecycle.ps1` — vendored verbatim from the scaffold.
- **`scripts/`** — `gen_ssl_cert.py` (HTTPS CA+leaf), `gen_token.py` / `set_password.py` (auth), `gen_icons.py` (PWA icons).
- **`spike/`** — `streamlit_app.py`, the independent POC spike.
- **`config/`** — `webapp_config.sample.json`, `display_names.sample.json`, `tuya_display_names.sample.json`, `security_display_names.sample.json`, `hvac_rules.sample.json`, `hvac_schedules.sample.json`, `location.sample.json`, `tariff.sample.json`, and `pv_system.sample.json` committed; the real `webapp_config.json`, `display_names.json`, `tuya_display_names.json`, `security_display_names.json`, `hvac_rules.json`, `hvac_schedules.json`, `location.json`, `tariff.json`, and `pv_system.json` are gitignored.
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
Each detector row shows a `Trouble` flag when set; tapping a detector's name
opens a detail modal showing its type, status, and trouble state, plus a
**Display name** field. Detectors arrive named `1`, `2`, … so a custom label is
saved via `PUT /api/security/zones/{id}/display_name` to a gitignored
`config/security_display_names.json` (zone id → label, parallel to the unit
`config/display_names.json` and plug `config/tuya_display_names.json`); a missing
file is not an error, and the override wins over the RISCO name everywhere.

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

Config in `.env`:

| Key | Meaning |
|-----|---------|
| `RISCO_USERNAME` | RISCO Cloud login email. Use a dedicated sub-account if possible so the dashboard does not compete with the phone app session. |
| `RISCO_PASSWORD` | RISCO Cloud password. |
| `RISCO_PIN` | Panel PIN used by RISCO Cloud/WebUI commands. |
| `RISCO_PERIMETER_GROUP` | Optional group letter used only to label partially-set states more precisely. |
| `RISCO_PARTIAL_GROUP` | Optional group letter used only to label partially-set states more precisely. |

Read-only smoke command:

```powershell
& .\.venv\Scripts\python.exe -m src.list_security
```

RISCO periodically blocks third-party clients. If login starts failing after
credentials were known-good, check for a newer `pyrisco` release before changing
the app code.

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

The PWA splits into five tabs: **Home** (a consolidated dashboard — weather strip,
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
valued at the avoided rate — see *Electricity tariff* below), and **🔌 Plugs** (the local Smart Life
devices — see below), and **🛡️ Security** (RISCO alarm controls, event log, and
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
  the webapp nudges the unit setpoint every 15 minutes (default) to keep the
  measured room temperature near the configured room target, with a 0.5 °C
  buffer. Auto/Fan modes are not steered.
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
