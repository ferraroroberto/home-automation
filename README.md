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
  - `tuya_client.py` — Smart Life / Tuya discovery and local LAN control foundation.
  - `webapp_config.py` — webapp host/port + auth secrets loader.
- **`app/webapp/`** — the FastAPI + PWA product.
  - `server.py` — `create_app()`, middleware, static mount, routers, sampler lifespan.
  - `middleware.py` — bearer-token / loopback auth gate.
  - `sampler.py` — background energy sampler owned by the webapp lifecycle.
  - `routers/` — `units` (read + control), `energy` (live flow + history/aggregate), `tuya` (local Smart Life devices + watts), `auth` (login), `misc` (page, health, CA profile).
  - `static/` — the PWA (HTML/CSS/ES-modules), `manifest.webmanifest`, icons.
    Modules: `main.js` (boot + AC cards), `tabs.js` (Home/AC/Energy/Plugs switcher),
    `energy.js` (energy tab + live polling), `plugs.js` (Smart Life tab),
    `charts.js` (Chart.js wrappers), `state.js`, `api.js`;
    `vendor/chart.umd.min.js` (vendored Chart.js v4).
- **`app/tray/`** — the Windows tray that owns the webapp lifecycle (`tray.bat`).
  - `tray.py` — pystray icon + menu; `__main__.py` — the `-m app.tray` entry.
  - `manager.py` — adopt-or-spawn / restart / stop for the uvicorn webapp.
  - `single_instance.py`, `tray_lifecycle.ps1` — vendored verbatim from the scaffold.
- **`scripts/`** — `gen_ssl_cert.py` (HTTPS CA+leaf), `gen_token.py` / `set_password.py` (auth), `gen_icons.py` (PWA icons).
- **`spike/`** — `streamlit_app.py`, the independent POC spike.
- **`config/`** — `webapp_config.sample.json` committed; real `webapp_config.json` gitignored.
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

## SMA solar / energy

The dashboard shows the home's live energy flow (☀️ Solar · 🏠 House · ⚡ Grid ·
♻️ Net) as the read-side foundation of the eventual solar load-balancing
automation (shift HVAC load to match PV). When `SMA_CLOUD_PLANT_ID` is set, it
uses the same Sunny Portal energy-balance values shown in the SMA Energy app.
If cloud is not configured or unavailable, it falls back to local LAN reads:

- **Sunny Home Manager 2.0 / energy meter** — read over **Speedwire** (UDP
  multicast) with **no credentials**. Gives grid import/export + cumulative
  counters. Discovered automatically on the LAN.
- **PV inverter** (Tripower X / ennexOS) — read over its **local ennexOS web
  API**, logging in with the SMA account. Gives PV production. SMA inverters
  **power down at night**, so the inverter only appears on the network while
  producing; an asleep inverter is reported as such (PV unknown), not an error.

Config in `.env`:

| Key | Meaning |
|-----|---------|
| `SMA_CLOUD_PLANT_ID` | Sunny Portal plant/component ID. When set, the app reads the same cloud energy-balance values shown in the SMA Energy app. |
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

The PWA splits into four tabs: **Home** (a compact energy tile + a read-only
one-line-per-unit AC summary), **AC** (the full unit controls + detail modal),
**Energy** (live hero numbers, a flowing line chart of recent
production/consumption/net, and hourly/daily/monthly aggregate bars), and
**🔌 Plugs** (the local Smart Life devices — see below).

While the webapp runs, a background **sampler** (started in the FastAPI
lifespan, so it lives and dies with the tray's uvicorn process) persists the
live energy flow to a server-side SQLite database. Recent raw samples feed the
live chart; completed hours are folded into compact rollups that daily and
monthly views group from. An **asleep inverter is stored as no PV reading**, not
a misleading 0, so the charts show a gap and aggregates flag `pv_missing`.

- **Storage:** `webapp/energy_history.sqlite3` (gitignored, per-machine runtime
  data — never committed).
- **Endpoints:** `GET /api/energy` (live snapshot), `GET /api/energy/history?minutes=N`
  (raw samples for the live chart), `GET /api/energy/aggregate?range=hourly|daily|monthly`
  (energy-per-bucket, Wh).
- **Cadence/retention knobs** (`.env`, all optional):

| Key | Default | Meaning |
|-----|---------|---------|
| `ENERGY_SAMPLER_ENABLED` | `true` | Master switch. `false`/`0` serves live + existing history but persists nothing (used by the e2e suite and dev runs so they don't poll SMA). |
| `ENERGY_PERSIST_INTERVAL_S` | `60` | Seconds between persisted samples. For a 5-minute cloud source (Sunny Portal) the data simply won't change faster than the source. |
| `ENERGY_COMPACT_INTERVAL_S` | `3600` | How often completed hours are folded into rollups and old raw data pruned. |
| `ENERGY_RAW_RETENTION_DAYS` | `7` | How long raw per-sample rows are kept (feeds the live chart). |
| `ENERGY_HOURLY_RETENTION_DAYS` | `400` | How long hourly rollups are kept (daily/monthly views group from these). |

The live display refreshes every ~10 s while the Energy tab is open and every
30 s otherwise; that cadence is a frontend constant (it never persists faster
than `ENERGY_PERSIST_INTERVAL_S`). Energy (Wh) is integrated from the samples
(rectangular rule, gaps capped) — a household-monitoring estimate, not a
billing-grade meter read.

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

The PWA's **Plugs** tab is a Smart-Life-style control surface for these local Tuya devices: a dense mobile grid showing every captured device at once, with an on/off switch where supported, **live wattage on metered plugs** (so solar/load decisions are obvious without opening the vendor app), and open/stop/close controls for covers. It is **cloud-free at runtime** — it reads `devices.json` plus local LAN status only.

- **Endpoints:** `GET /api/tuya` (device cards with switch state, reachability, and live energy fields — the per-device LAN reads run in parallel), `POST /api/tuya/{id}/switch` (`{"on": true|false}`), `POST /api/tuya/{id}/cover` (`{"action": "open"|"close"|"stop"}`).
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

## CLI

Print every device's live state:

```powershell
& .\.venv\Scripts\python.exe -m src.list_devices                  # Windows
```

```bash
./.venv/bin/python -m src.list_devices                            # POSIX
```

## Tests

A Playwright browser-E2E suite lives in `tests/e2e/`. It boots the real
webapp (adopting a running one on :8447, else autobooting a disposable
instance with the energy sampler off) and drives the PWA, **stubbing
`/api/units`, the `/api/energy*` endpoints, and `/api/tuya*` with fixtures** so
it never touches the live cloud, the LAN, or actuates real HVAC. Coverage
includes the Home/AC/Energy/Plugs tab navigation, the read-only AC summary, an
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
