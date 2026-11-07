# Tesla (read-only)

The app reads the household's Tesla through the **Tesla Fleet API directly** and keeps what it reads in a local SQLite store: samples, drives and charging sessions, for analysing kilometres driven and energy charged. It is read-only by construction. No command, wake-up or write endpoint exists in the code, and the OAuth scopes requested include no command scope.

## Route and why

| Route | Verdict |
| --- | --- |
| **Fleet API direct** | **Chosen.** No third party holds the vehicle data. Pay-per-use within the account's monthly discount. Polling needs no inbound path, which fits a tailnet-only app. |
| Teslemetry (hosted proxy) | Fallback only if Tesla refuses the app registration, and only after the owner agrees: it is a subscription and a third party holds the data. |
| Home Assistant Tesla Fleet integration | Not installed in this HA instance (no Tesla entities), and it needs the same developer app plus HA in the data path. |
| TeslaMate | Adds Postgres and Docker, and keeps the data outside this app's own store. |

Streaming (Fleet Telemetry) would cost less per signal, but the car has to reach a public HTTPS endpoint with mutual TLS, and this app is reachable only over Tailscale ([`webapp-access.md`](webapp-access.md)). The app polls instead.

## Region and endpoints

- Fleet API base (Europe, including Spain): `https://fleet-api.prd.eu.vn.cloud.tesla.com` (`TESLA_FLEET_BASE_URL` overrides it).
- Login: `https://auth.tesla.com/oauth2/v3/authorize`; tokens: `https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token`.
- Scopes: `openid offline_access vehicle_device_data vehicle_location`. Location is requested only so the sampler can tell home from away; see [Privacy](#privacy).

## Cost

Tesla's published pricing for the Fleet API is pay-per-use: **Data** requests at 500 per $1, **Commands** at 1,000 per $1, **Wakes** at 50 per $1, and **streaming signals** at 150,000 per $1. Each account gets a **$10 monthly discount**, and every response with a status below 500 is billed. Rate limits per device are 60 data requests per minute, 3 wakes per minute and 30 commands per minute. Source: `developer.tesla.com` and its `docs/fleet-api/billing-and-limits` page. Whether EU VAT applies on top is not stated there; confirm on the first invoice.

The app uses only Data requests (never Wakes or Commands), and its default cadence is sized to stay inside the discount:

| Car is | Requests | Default interval | Monthly estimate |
| --- | --- | --- | --- |
| asleep, offline, or idle between reads | 1 state check | 900 s | ~2,900 requests, ~$5.80 |
| driving or charging | 1 `vehicle_data` (no separate state check) | 180 s | ~1 h driving a day plus ~8 five-hour charges a month: ~1,400 requests, ~$2.80 |
| online and idle | state check + `vehicle_data` | 900 s, then a 1,200 s pause after 900 s idle | small |

A total around $8–9 a month fits inside the $10 discount. Every read logs `🚗 Tesla <state> (requests today: N)` at info level in the webapp log, so the real volume is visible. If it runs high, raise `TESLA_IDLE_INTERVAL_S` first.

## Never waking the car

- Every idle read first asks for the vehicle's state, which Tesla answers without contacting the car. Only an `online` car gets a `vehicle_data` read.
- While the car is driving or charging, the sampler reads `vehicle_data` directly. A car that has fallen asleep since answers with HTTP 408 and is not woken; the app records it as asleep.
- Data reads reset the car's sleep timer, so after 15 min idle the sampler stops reading data for 20 min (state checks only), long enough for the car to fall asleep.
- `asleep` and `offline` are states, not errors. They log at info level and store nothing but the status row.

## Store

`tesla_history.sqlite3` in the fleet runtime-data folder (`TESLA_HISTORY_DB_PATH` overrides it), through `src/_sqlite.py`:

- `samples` — battery %, range (km), charging state, charger power (kW), energy added (kWh), odometer (km), gear, speed (km/h), `at_home`. Kept `TESLA_RAW_RETENTION_DAYS` (90).
- `drives` — start/end, km from the odometer, battery % at each end, and energy estimated as the %-drop times `TESLA_USABLE_KWH` (60, a Model Y RWD). Built from consecutive parked reads, so a short trip the poll missed still counts when the odometer moved.
- `charging_sessions` — start/end, kWh added (the car's own per-session counter, final value from the closing read), peak kW, battery % at each end, home/away/unknown, and `source` (`car`).
- `status` — the last connectivity state and when it was checked.

Weekly and monthly figures (km, drive kWh, charged kWh, home-charged kWh, kWh per 100 km) are grouped from drives and sessions on read, in Europe/Madrid calendar weeks and months.

`python -m src.list_tesla stored` prints the latest sample, the recent drives and charging sessions, and the monthly totals straight from the store, with no Tesla call.

## Read API, the car card and the history sheet

`GET /api/tesla` (`app/webapp/routers/tesla.py`) serves the store and never calls Tesla, so reading it costs no Fleet API request. It returns `configured` (the app identity and an owner login are present), `sampler_enabled`, `usable_kwh`, the latest stored `sample`, and the last connectivity `state` with its `checked_at`. The client's error text is not passed on. Until the car is configured the store is not opened and the route answers `configured: false`. `GET /api/tesla/history?period=week|month` returns the per-period `buckets` (oldest first, each with `km`, `drive_kwh`, `drives`, `charged_kwh`, `charged_home_kwh`, `charging_sessions` and `kwh_per_100km`) plus `sampler_enabled`; `GET /api/tesla/sessions?limit=` (1–100, default 10) returns the latest `drives` and `charging` sessions, newest first, with a home/away/unknown `location` and no coordinates. Both answer `configured: false` with empty lists until the car is set up. A test fails if a non-GET route is added under `/api/tesla`.

The Devices tab's **Car** card (`static/devices/tesla.js`, `styles/tesla.css`) polls `GET /api/tesla` every minute while Devices is open. It shows battery % with a level meter, range, the charging state (with power and energy added while charging), and last seen: the state word and the age of the latest reading, with the avatar's alive badge only while the car answered online. The card stays hidden while the car is not configured. Once it is configured but has no stored reading, it shows an empty state that says whether collection (`TESLA_SAMPLER_ENABLED`) is off. When a refresh fails after a good read, the figures stay on screen and the header says when they were last updated. The card has no controls; its one link opens the history sheet.

The **Car history** sheet (`static/devices/tesla-history.js`, markup in `views/devices-dialogs.html`) reads `/api/tesla/history` and `/api/tesla/sessions` each time it opens and when the Week/Month toggle changes, never on a timer. It shows the current period's distance, charged energy and efficiency (kWh per 100 km), a bar chart of km and a stacked bar chart of charged kWh split home and away for the last 12 weeks or months (a period with no activity is an empty bar), and the latest five charging sessions and drives. Drive energy is the battery drop times `TESLA_USABLE_KWH`, so efficiency is an estimate; home-charging kWh is the car's own counter until the home charger is identified. With no stored drive or session it shows an empty state that says whether collection is off.

## Privacy

The repo is public. The client secret is in `.env`; the OAuth tokens are in the gitignored `config/tesla_tokens.json`; neither is ever logged. The VIN is never stored or logged (`TESLA_VIN` is only needed for an account with more than one car). Coordinates are compared against the home location in `config/location.json` in memory and dropped; only the `at_home` flag is stored. The home radius is `TESLA_HOME_RADIUS_M` (150).

## Owner steps

The code ships and is tested without any of these; live data waits on them.

1. **Developer account and app.** Sign in at `developer.tesla.com` with the Tesla account that owns the car and create an application. Choose the read-only scopes (vehicle information, vehicle location), set an allowed redirect URI, and copy the client id and secret into `.env` (`TESLA_CLIENT_ID`, `TESLA_CLIENT_SECRET`, `TESLA_REDIRECT_URI`). Record here whether Tesla approved the app at once or asked for review: individual-developer approval is unconfirmed until this step runs.
2. **Public-key domain.** Generate an EC key pair (prime256v1), keep the private key outside the repo, and publish the public key at `https://<domain>/.well-known/appspecific/com.tesla.3p.public-key.pem`. Any static host works, for example a GitHub Pages site with a `.nojekyll` file so the dot-folder is served. Put the domain in `TESLA_DOMAIN`, then register it for the EU region once: `python -m src.list_tesla register`. Read-only access needs no virtual key on the car.
3. **Login.** `python -m src.list_tesla auth-url`, open the printed URL, log in and approve; copy the `code` parameter from the redirect and run `python -m src.list_tesla auth-code <code>`. The tokens land in `config/tesla_tokens.json` and refresh themselves.
4. **Probe, then enable.** `python -m src.list_tesla probe` prints the state and, if the car is awake, the sample it would store. Then set `TESLA_SAMPLER_ENABLED=1` in `.env` and restart the tray (`tray.bat --restart`).
5. **Identify the home charger.** The make and model are unknown: the owner reported it by voice as something like "mod boost". It is not on the LAN under a recognisable name, and no Athom clamp measures its circuit. Until it is identified, home-charging kWh comes from the car's own `charge_energy_added`. Once known, check whether it has a local or cloud API with delivered kWh per session; if it does, it becomes the home-charging source (`source = 'charger'`) in a follow-up issue.
