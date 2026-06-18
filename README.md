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
  - `webapp_config.py` — webapp host/port + auth secrets loader.
- **`app/webapp/`** — the FastAPI + PWA product.
  - `server.py` — `create_app()`, middleware, static mount, routers.
  - `middleware.py` — bearer-token / loopback auth gate.
  - `routers/` — `units` (read + control), `auth` (login), `misc` (page, health, CA profile).
  - `static/` — the PWA (HTML/CSS/ES-modules), `manifest.webmanifest`, icons.
- **`app/tray/`** — the Windows tray that owns the webapp lifecycle (`tray.bat`).
  - `tray.py` — pystray icon + menu; `__main__.py` — the `-m app.tray` entry.
  - `manager.py` — adopt-or-spawn / restart / stop for the uvicorn webapp.
  - `single_instance.py`, `tray_lifecycle.ps1` — vendored verbatim from the scaffold.
- **`scripts/`** — `gen_ssl_cert.py` (HTTPS CA+leaf), `gen_token.py` / `set_password.py` (auth), `gen_icons.py` (PWA icons).
- **`spike/`** — `streamlit_app.py`, the independent POC spike.
- **`config/`** — `webapp_config.sample.json` committed; real `webapp_config.json` gitignored.
- **`webapp/`** — runtime state (`certificates/`, `auth.log`); gitignored.
- **`.env`** — MELCloud credentials (gitignored; copy from `.env.example`).

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
instance) and drives the PWA, **stubbing `/api/units` with fixtures** so
it never touches the live cloud or actuates real HVAC. Runs in two
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
