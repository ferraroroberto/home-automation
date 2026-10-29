# home-automation

Control your Mitsubishi Electric units from your phone — a mobile-first, installable **PWA** over **MELCloud Home**, ahead of building a solar load-balancing automation on top of it.

> **Platform note.** These units migrated from classic MELCloud (`app.melcloud.com`) to **MELCloud Home**, which is a different API. The classic `pymelcloud` library cannot see them. This project uses [`aiomelcloudhome`](https://github.com/erwindouna/aiomelcloudhome) — a pure-async client that does the PKCE login over HTTP (no browser). Use your **MELCloud Home** credentials in `.env`.

The product is a **FastAPI + static PWA**: one row per unit (its mode glyph, the room → set temperatures, and the power switch one tap away); tapping a row opens the unit sheet, which sends each change as it is made — a large setpoint stepper, power, mode and fan as segmented controls, then the vanes, the temperature rule, the schedules and the name. A unit whose WiFi adapter has lost its cloud connection (MELCloud Home's own `isConnected` flag) shows **an "Offline" chip and a red avatar badge**, and every control that would write to it — the row's power switch, and the sheet's setpoint, power, mode, fan and vanes — is disabled rather than silently swallowing the command; its last-known readings stay legible, and the locally-stored settings (display name, temperature rule, schedules) remain editable. It is reachable over **Tailscale**, behind a real Let's Encrypt HTTPS endpoint (`tailscale cert`) and an optional bearer token. Two ways to reach it once running:

- **Tailscale** (anywhere on the tailnet, including this PC): `https://<pc>.<tailnet>.ts.net:8447` — trusted cert, no per-device setup
- **Loopback** (plain desktop access on the PC): `http://localhost:8447` — plain HTTP; `https://localhost` would warn (the cert is for the `.ts.net` name — see [Webapp access](docs/webapp-access.md))

> The **Streamlit app is a POC spike** (`spike/streamlit_app.py`) — a
> throwaway data/debug view, independent from the product. See
> [Streamlit spike](#streamlit-spike-poc).

## Layout

The top-level directory map — what each directory *is*. The exhaustive module-by-module reference (every file in `src/` and `app/webapp/`, one line each) lives in [`docs/architecture.md`](docs/architecture.md). Where new code goes, and the structure ratchet that holds it there: [`docs/layout.md`](docs/layout.md).

- **`src/`** — non-UI Python: the device clients (MELCloud HVAC, Huawei solar, RISCO alarm, Tuya, cameras, network, UPS, Elgato, presence), their `list_*` CLIs, the automation/tariff/forecast logic, and the atomic display-name / hidden / preference stores. No Streamlit/FastAPI imports.
- **`app/webapp/`** — **the product**: FastAPI (`server.py` + `middleware.py` + `routers/`) over the same core, serving the static PWA under `static/`, plus the lifecycle `manager.py` and the background tasks (energy sampler, HVAC/security/presence/wake-alarm automation, power monitor, SearXNG watchdog) owned by the app lifespan.
- **`app/tray/`** — the Windows tray that owns the webapp lifecycle (`tray.bat` → `python -m app.tray`); `single_instance.py` vendored verbatim from the scaffold. `tray_lifecycle.ps1` is never vendored per-app — `tray.bat` shells out to the one shared, machine-local copy fleet-config installs at `%USERPROFILE%\.claude\tray\tray_lifecycle.ps1`.
- **`custom_components/home_automation_app/`** — Home Assistant custom integration: a thin adapter over the `/api/*` endpoints exposing native `climate` / `switch` / `alarm_control_panel` / `binary_sensor` / `sensor` entities. See [`docs/home-assistant-integration/`](docs/home-assistant-integration/README.md).
- **`scripts/`** — ops helpers: `gen_tailscale_cert.py` (HTTPS cert), `gen_token.py` / `set_password.py` (auth), `gen_web_push_keys.py`, `gen_icons.py`, `ha_config_sync.py` (voice-PE HA config deploy over SSH; see [Voice control](docs/voice-control.md)).
- **`spike/`** — `streamlit_app.py`, the independent Streamlit POC spike.
- **`config/`** — committed `*.sample.json` templates; the real per-feature JSON stores are gitignored.
- **`webapp/`** — runtime state (`certificates/`, `auth.log`, `webapp.log` — the rotating process log that survives the tray's DEVNULL launch, `slow-requests.log` — one line per over-budget request, the SQLite stores); gitignored.
- **`.env`** — every integration's credentials (gitignored; copy from `.env.example`, which lists one key block per integration — MELCloud, FusionSolar, RISCO, Tuya, Huawei Modbus, network router/AP, iCloud presence, Home Assistant, Google Calendar, SearXNG, Elgato, Athom, and more). Each block is optional: the feature it backs degrades gracefully when its keys are absent, per [Feature reference](#feature-reference).

### Background polling (what fetches when)

The PWA does **not** poll everything continuously. Each tab's data is fetched only while that tab is active (the LAN/cloud reads are comparatively expensive); leaving a tab stops its timer. The matrix below is the contract — keep it in sync when cadences change.

| Data | Cadence | Polls while on | Notes |
| --- | --- | --- | --- |
| AC units | 30 s | Home, AC | One boot fetch on load; otherwise gated to these tabs. |
| Energy | 5 s active / 30 s slow | Energy (fast), Home (slow) | Served from one cached cloud read. |
| Plugs | 15 s | Devices | Tuya LAN reads. |
| Circuits | 15 s | Devices | Per-circuit Athom CT-clamp reads. |
| UPS | 15 s | Devices, Home | Local NUT/USB-HID read. |
| PC fleet | 15 s | Devices | Machine roster via the local hub proxy. |
| Lights | 15 s | Devices | Elgato LAN reads. |
| Network | once on entry, then 15 s | Devices (once), Network sheet (poll) | AP SOAP + router reads: one read on entering Devices fills the Network group, then polled only while the Network sheet is open; speed test is button-only unless the opt-in nightly run is on (off by default). |
| Security | 10 s | Security, Home | RISCO cloud. |
| HA VM | 30 s | Devices | Hyper-V `Get-VM` on the host. |
| HA / Voice PE | once on entry, then 15 s | Devices (once), its sheets (poll) | Satellite state + recent interactions: polled only while the Voice satellites or Recent interactions sheet is open. |
| SearXNG status | 30 s | Settings | Search engine card. |
| Weather | ~10 min | (always) | Barely moves. |
| Build version | 5 min | (always) | Cheap; drives the build-identity footer. |

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

`.env.example` lists every integration's keys — one block per integration
(RISCO, Tuya, Huawei Modbus, network router/AP, iCloud presence, Home
Assistant, Google Calendar, SearXNG, Elgato, Athom, and more) — and each is
optional: the feature it backs degrades gracefully when its keys are absent.
At minimum, set `MELCLOUD_EMAIL` and `MELCLOUD_PASSWORD` (your MELCloud Home
login) to run the core HVAC product; fill in the other blocks only for the
features you actually use, per [Feature reference](#feature-reference) for
what each group enables.

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
  cleanly — no orphaned process on `:8447`). **Open** and **Copy Tailscale URL**
  both target the full tailnet FQDN
  (`https://<pc>.<tailnet>.ts.net:8447?token=…`) — the name the Let's Encrypt
  cert is issued for, so the lock is green on this PC too with no certificate
  fuss — falling back to the loopback URL only when no tailnet host resolves.

The tray launches `python -m app.tray`; detection/kill is scoped to *this*
repo's `.venv` by command line, so sister-app trays are never touched.

After a restart, confirm the *new* code is actually live (a `/healthz` 200 is
not enough — a stale process still passes it): the full **restart + build-identity
verification contract** — `GET /api/version` vs `git rev-parse --short HEAD`, the
PWA `Build:` footer, the 6-unit grid confirm — is the agent-facing contract and
lives in one place, [`CLAUDE.md`](CLAUDE.md) under **Restart recipe**.

### Headless / dev (no tray)

```powershell
.\webapp.bat                                                      # Windows
```

`webapp.bat` binds `0.0.0.0:8447` and serves **HTTPS** when
`webapp/certificates/cert.pem` exists (see [Webapp access](docs/webapp-access.md)),
otherwise plain HTTP. Invoke uvicorn directly if you prefer:

```powershell
& .\.venv\Scripts\python.exe -m uvicorn app.webapp.server:app --host 0.0.0.0 --port 8447 `
    --ssl-keyfile webapp/certificates/key.pem --ssl-certfile webapp/certificates/cert.pem
```

The signal that new code is live is the `GET /api/version` `git_sha` matching `git rev-parse --short HEAD` (a `/healthz` 200 alone is not enough — a stale process passes it); the 6-unit grid rendering is only a secondary visual confirm.

**Automation engines run only in the live instance.** The alarm and blind schedules, presence and HVAC automation, wake alarms, the power monitor, samplers, the SearXNG watchdog and the nightly speed test act on the real alarm, AC units, blinds and household notifications. So they start only when the launcher sets `HOME_AUTOMATION_ENGINES=1`, which the tray's `WebappManager` and `webapp.bat` (the two launchers of the live `:8447` instance) do. Any other boot (a sibling worktree, another port, the bare `uvicorn` line above, the e2e autoboot) serves the API/PWA with **no engine at all**, and its first startup log line says `automation engines off — …`. Where it is set, the single-owner ownership lock still decides which single process runs them. To opt a dev instance in deliberately, set the variable in that one shell, e.g. `$env:HOME_AUTOMATION_ENGINES = "1"` before the `uvicorn` line, and know that it will then act on the real devices. **Never put it in `.env`**: `.env` is copied into every worktree, so the webapp refuses an opt-in it finds there and logs why.

## Verification gate

One command runs byte-compile, the static, docs, comment, structure, instruction, doc-refs, dead-code and conventions ratchets and the backend suite, then routes the browser (e2e) phase to the size of the diff (docs-only skips it; a real UI change runs Chromium + WebKit):

```powershell
& .\scripts\verify-before-ship.ps1
```

Install the test dependencies first: `& .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt`. Suites, routing, runtime budgets and the live-adoption guard: [`docs/testing.md`](docs/testing.md).

## Feature reference

One line per feature; the reference for each (behaviour, config, endpoints, invariants) lives in `docs/`.

| Feature | Reference |
| --- | --- |
| HVAC automation (temperature rules, schedules) and custom unit names | [`docs/hvac.md`](docs/hvac.md) |
| RISCO alarm, Security tab, alarm scene capture + AI verdict, auto-bypass override | [`docs/security-alarm.md`](docs/security-alarm.md) |
| Presence (iCloud Find My, webhooks, home location), alarm automation, family locator, Web Push | [`docs/presence.md`](docs/presence.md) |
| Home network: devices, MAC pinning, Wi-Fi walk test, DHCP reservation plan | [`docs/network.md`](docs/network.md) · [`docs/network-spike.md`](docs/network-spike.md) |
| Huawei solar / energy flow (Modbus first, FusionSolar cloud fallback) | [`docs/huawei-energy.md`](docs/huawei-energy.md) |
| Per-circuit monitoring (Athom CT clamps) | [`docs/circuits.md`](docs/circuits.md) |
| Energy history, daily rollups and the electricity tariff | [`docs/energy-history.md`](docs/energy-history.md) · [`docs/tariff-model.md`](docs/tariff-model.md) |
| Weather and solar forecast | [`docs/weather-solar-forecast.md`](docs/weather-solar-forecast.md) · [`docs/pv-forecast.md`](docs/pv-forecast.md) |
| Tuya / Smart Life plugs, covers and the Devices tab | [`docs/tuya.md`](docs/tuya.md) |
| Elgato lights | [`docs/elgato-lights.md`](docs/elgato-lights.md) |
| Cameras | [`docs/cameras.md`](docs/cameras.md) · [`docs/camera-spike.md`](docs/camera-spike.md) |
| Wake alarms and timers, reminders, calendar | [`docs/timers-reminders-calendar.md`](docs/timers-reminders-calendar.md) |
| Voice control (hands-free, fully local), Voice PE satellites | [`docs/voice-control.md`](docs/voice-control.md) · [`docs/voice-commands-howto.md`](docs/voice-commands-howto.md) · [`docs/voice-pe-config/`](docs/voice-pe-config/README.md) · [`docs/voice-model-benchmark.md`](docs/voice-model-benchmark.md) |
| Home Assistant VM and the custom integration | [`docs/home-assistant-vm.md`](docs/home-assistant-vm.md) · [`docs/home-assistant-integration/`](docs/home-assistant-integration/README.md) |
| Search engine (SearXNG) — voice web-search backend | [`docs/search-engine.md`](docs/search-engine.md) |
| Activity log and usage log | [`docs/activity-usage-logs.md`](docs/activity-usage-logs.md) |
| HTTPS (Tailscale cert), PWA install, token + password auth | [`docs/webapp-access.md`](docs/webapp-access.md) |
| Generalized action alias (Stream Deck and similar callers) | [`docs/action-alias.md`](docs/action-alias.md) |
| Tests: backend suite, browser e2e, runtime budgets | [`docs/testing.md`](docs/testing.md) · [`docs/playwright-ui-testing.md`](docs/playwright-ui-testing.md) |
| Module-by-module inventory and the structure diagram | [`docs/architecture.md`](docs/architecture.md) · [`docs/architecture.mmd`](docs/architecture.mmd) |

## CLI

Print every device's live state:

```powershell
& .\.venv\Scripts\python.exe -m src.list_devices                  # Windows
```

```bash
./.venv/bin/python -m src.list_devices                            # POSIX
```

Print live per-circuit power from every discovered CT-clamp meter:

```powershell
& .\.venv\Scripts\python.exe -m src.list_circuits                 # Windows
```

```bash
./.venv/bin/python -m src.list_circuits                           # POSIX
```

Print visible iCloud Find My presence entities:

```powershell
& .\.venv\Scripts\python.exe -m src.list_presence                 # Windows
```

```bash
./.venv/bin/python -m src.list_presence                           # POSIX
```

## Streamlit spike (POC)

A lightweight, **throwaway** data/debug view — independent from the product,
sharing only `src/devices/melcloud_client.py`. Not the real UI; kept only as a fast
way to eyeball the data.

```powershell
.\launch_app.bat                                                  # Windows (http://localhost:8501)
```

```bash
./launch_app.sh                                                   # POSIX
```

…or directly: `& .\.venv\Scripts\python.exe -m streamlit run spike/streamlit_app.py`.
