# home-automation

Control your Mitsubishi Electric units from your phone — a mobile-first, installable **PWA** over **MELCloud Home**, ahead of building a solar load-balancing automation on top of it.

> **Platform note.** These units migrated from classic MELCloud (`app.melcloud.com`) to **MELCloud Home**, which is a different API. The classic `pymelcloud` library cannot see them. This project uses [`aiomelcloudhome`](https://github.com/erwindouna/aiomelcloudhome) — a pure-async client that does the PKCE login over HTTP (no browser). Use your **MELCloud Home** credentials in `.env`.

The product is a **FastAPI + static PWA**: a card grid showing every unit at once, each card carrying the everyday controls inline (on/off, target temperature, fan speed, room-temperature readout); a per-unit detail modal holds the rest (operation mode + the two vanes). It is reachable over **Tailscale**, behind a real Let's Encrypt HTTPS endpoint (`tailscale cert`) and an optional bearer token. Two ways to reach it once running:

- **Tailscale** (anywhere on the tailnet, including this PC): `https://<pc>.<tailnet>.ts.net:8447` — trusted cert, no per-device setup
- **Loopback** (plain desktop access on the PC): `http://localhost:8447` — plain HTTP; `https://localhost` would warn (the cert is for the `.ts.net` name — see [HTTPS](#https-tailscale-cert))

> The **Streamlit app is a POC spike** (`spike/streamlit_app.py`) — a
> throwaway data/debug view, independent from the product. See
> [Streamlit spike](#streamlit-spike-poc).

## Layout

The top-level directory map — what each directory *is*. The exhaustive module-by-module reference (every file in `src/` and `app/webapp/`, one line each) lives in [`docs/architecture.md`](docs/architecture.md).

- **`src/`** — non-UI Python: the device clients (MELCloud HVAC, SMA solar, RISCO alarm, Tuya, cameras, network, UPS, Elgato, presence), their `list_*` CLIs, the automation/tariff/forecast logic, and the atomic display-name / hidden / preference stores. No Streamlit/FastAPI imports.
- **`app/webapp/`** — **the product**: FastAPI (`server.py` + `middleware.py` + `routers/`) over the same core, serving the static PWA under `static/`, plus the lifecycle `manager.py` and the background tasks (energy sampler, HVAC/security/presence/wake-alarm automation, power monitor) owned by the app lifespan.
- **`app/tray/`** — the Windows tray that owns the webapp lifecycle (`tray.bat` → `python -m app.tray`); `single_instance.py` + `tray_lifecycle.ps1` vendored verbatim from the scaffold.
- **`custom_components/home_automation_app/`** — Home Assistant custom integration (#235): a thin adapter over the `/api/*` endpoints exposing native `climate` / `switch` / `alarm_control_panel` / `binary_sensor` / `sensor` entities. See [`docs/home-assistant-integration/`](docs/home-assistant-integration/README.md).
- **`scripts/`** — ops helpers: `gen_tailscale_cert.py` (HTTPS cert), `gen_token.py` / `set_password.py` (auth), `gen_web_push_keys.py`, `gen_icons.py`, `ha_config_sync.py` (voice-PE HA config deploy over SSH; see [Home Assistant config deploy](#home-assistant-config-deploy-over-ssh)).
- **`spike/`** — `streamlit_app.py`, the independent Streamlit POC spike.
- **`config/`** — committed `*.sample.json` templates; the real per-feature JSON stores are gitignored.
- **`webapp/`** — runtime state (`certificates/`, `auth.log`, the SQLite stores); gitignored.
- **`.env`** — MELCloud + SMA credentials (gitignored; copy from `.env.example`).

### Background polling (what fetches when)

The PWA does **not** poll everything continuously. Each tab's data is fetched only while that tab is active (the LAN/cloud reads are comparatively expensive); leaving a tab stops its timer. The matrix below is the contract — keep it in sync when cadences change.

| Data | Cadence | Polls while on | Notes |
| --- | --- | --- | --- |
| AC units | 30 s | Home, AC | One boot fetch on load; otherwise gated to these tabs (#209). |
| Energy | 5 s active / 30 s slow | Energy (fast), Home (slow) | SMA reads are lightweight. |
| Plugs | 15 s | Plugs | Tuya LAN reads. |
| UPS | 15 s | Plugs, Home | Local NUT/USB-HID read. |
| Lights | 15 s | Lights | Elgato LAN reads. |
| Network | 15 s | Network | AP SOAP + router reads; speed test is button-only. |
| Security | 10 s | Security, Home | RISCO cloud. |
| HA VM | 30 s | Home | Hyper-V `Get-VM` on the host (#240). |
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

**Per-detector data (issue #84).** The RISCO Cloud API does not expose
per-detector battery — only a generic per-zone *trouble* boolean (surfaced via the
Trouble indicator below). The system-wide low-battery flag was a noisy/sticky
proxy and was removed in #227; per-detector battery/connection differentiation
would need the panel's local interface, which isn't exposed (investigated in #220).

**AC-power-lost alert (issue #99).** When the panel loses mains power and runs on
backup battery, a red **`AC power lost`** badge (warning icon + text) appears on the `Alarm state`
line (Home + Security tiles) from the aggregate `ac_lost` flag, clearing when
mains power returns.

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

**Ignoring a detector's trouble (issue #225).** A detector's `Trouble` flag rolls
up to a **`Trouble (N)`** badge (warning icon + count) on the Home/Security `Alarm state` line counting
detectors that are troubled. For a known/accepted trouble (a detector that can't be
serviced yet), the detail modal has an **Ignore trouble** toggle: an ignored
detector reads muted **`Trouble — ignored`** in the list and is dropped from the
main-card count, so muting the known ones quiets the card while a new/un-ignored
trouble still shows there. Saved via `PUT /api/security/zones/{id}/trouble_ignored`
to a gitignored `config/security_trouble_ignore.json` (same atomic store; surfaced
as `trouble_ignored` per zone on `GET /api/security`). Differentiating the trouble
*cause* (battery vs comms) per detector isn't possible from the cloud — see #220.

**Resilient live read (issue #98).** A momentary panel-unreachable blip — RISCO
returns a non-retryable result code such as `26` on the *live* state read even
though the login, site, and PIN steps all succeed — no longer blacks out the
tab. `fetch_security_state()` falls back to the **cloud-cached** snapshot
(`fromControlPanel=False`), so the alarm state, the detector list, and the
trouble flags keep rendering and the action pills stay actionable.
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
`Partial`, `Perimeter`, or `Full`. Saved schedules appear as compact summary
rows; tap a row or **Add schedule** to edit in a dialog, where **Save** commits
the change and closing discards it. The tray-owned webapp evaluates these entries
server-side, fires each entry at most once per calendar day, and logs failed
alarm commands without stopping the scheduler so a transient RISCO error can be
retried on the next poll. The entries live in gitignored
`config/security_schedules.json`; copy `config/security_schedules.sample.json`
for the persisted shape if editing by hand.

**Automatic-alarm notifications (Telegram) — issue #231.** The Alarm tab has a
collapsible **Notifications** card (folded by default, just below Presence) that
pushes a short Telegram message when the home **automatically** arms or disarms —
and, by default, whenever an automatic attempt **fails** (e.g. the panel is
offline during a power cut, the incident that motivated this), plus the two
adverse panel events (intrusion + panel mains-power loss). Seven independent
toggles, persisted to gitignored `config/alarm_notify_prefs.json`
(`config/alarm_notify_prefs.sample.json` committed), saved via
`GET`/`PUT /api/security/notify-prefs`:

| Toggle | Fires when |
|--------|-----------|
| Automatic arm (schedule) | a weekly schedule armed the alarm |
| Automatic disarm (schedule) | a weekly schedule disarmed the alarm |
| Arm on everyone-away (presence) | presence automation armed (everyone left) |
| Disarm on arrival (presence) | presence automation disarmed (someone arrived) |
| Error on any automatic arm/disarm | an automatic attempt failed (carries the panel's error text) |
| Alarm triggered (intrusion) | the panel goes into ongoing/memory alarm (🚨) |
| Panel mains power lost/restored | the panel's `ac_lost` flag flips either way (⚠️/✅) |

**Defaults:** the four arm/disarm *success* toggles are **off** (opt-in); the three adverse ones — `error`, `intrusion`, `ac_lost` — are **on**. The intrusion + panel-AC alerts are edge-triggered off the live RISCO state read by the presence-automation loop, so they require that loop to be running (`PRESENCE_AUTOMATION_ENGINE_ENABLED`, default on); it is the only interval reader of RISCO state, so reusing it avoids a second poller hitting the cloud's third-party rate limit. The `ongoing_alarm`/`memory_alarm` panel flags behind the intrusion alert come from an undocumented, screen-scraped RISCO WebUI endpoint; a poll where that scrape comes back unreadable is treated as *unknown*, not *cleared* (issue #307) — otherwise the next successful poll re-observing a still-latched, days-old `memory_alarm` manufactures a false "new" intrusion alert with no attributable zone. Each intrusion log line also carries the raw flag values as a `diagnostic` field (log-only, not in the Telegram copy) so a future occurrence is diagnosable without re-investigating. **Manual** arm/disarm from the app's own buttons is never
notified (you're already looking at the app). A persistently-failing automatic
action (an offline panel retried every poll) alerts **once per day**, not every
poll. Telegram delivery is best-effort: a delivery failure is logged and never
breaks the automation loop. Configure credentials by copying
`config/notify_config.sample.json` to gitignored `config/notify_config.json` and
filling in `bot_token` + `chat_id` (or set `TELEGRAM_BOT_TOKEN` /
`TELEGRAM_CHAT_ID` in `.env`, which take precedence); with no credentials the
notifier is a silent no-op. The notifier itself is the universal
`src/notify/` component vendored verbatim from `project-scaffolding`.

**Local activity log.** Independently of Telegram, **every** alarm command the
app issues — schedule, presence, *and* manual — is appended with its result
(`ok` / `error` + the error text) to gitignored `logs/alarm.jsonl`, a local
alternative to the RISCO cloud event log so you can see what was attempted, how
often, and whether it worked. It is written through the reusable
`src.activity_log.append_activity(consumer, event)` facility (one append-only
JSONL writer, `logs/<consumer>.jsonl`), which the presence trigger log
(`logs/presence_triggers.jsonl`) now also routes through. `source: manual`
entries also carry an `actor` field — `webapp` (the PWA's own Security tab,
the default when no header is sent), `ha` (the Home Assistant integration), or
`voice-pe` (the ESPHome voice bridge) — so an unexpected manual arm/disarm can
be traced to its caller without guessing (issue #405). RISCO's own cloud event
log cannot make this distinction: every command, automated or manual, goes out
under the same dedicated WebUI account.

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
| `TELEGRAM_BOT_TOKEN` | Optional; Telegram Bot API token for automatic-alarm notifications. Overrides `config/notify_config.json`. Unset → notifier is a silent no-op. |
| `TELEGRAM_CHAT_ID` | Optional; Telegram chat id the alerts are delivered to (the family-radar chat). Overrides `config/notify_config.json`. |

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

**Re-authenticating after a login failure (issue #442).** `GET /api/presence` diagnostics (`reason`) tell you what broke: `2fa_required` (the trusted session expired — just rerun the 2FA smoke command above), `not_configured` (`.env` is missing `ICLOUD_EMAIL`/`ICLOUD_PASSWORD`), or `error` with a detail message. A detail of *"Invalid email/password combination"* usually means the Apple ID password changed since the session was trusted — update `ICLOUD_PASSWORD` in `.env` and rerun `src.list_presence`. If Apple's response instead says the account was **locked for security reasons** (a real failure mode: the background refresher retries every `PRESENCE_ICLOUD_REFRESH_INTERVAL_S`, so a stale password can trip Apple's own repeated-failed-login lock), that's an Apple-side hold with no code-side fix — unlock/reset the account at [iforgot.apple.com](https://iforgot.apple.com) first, then retry the CLI. `POST /api/presence/refresh` (or the CLI) re-runs the same login once the credentials/account are good again.

Spike recommendation: use this read path only if the session survives unattended for weeks and returns the two phones reliably. If 2FA/session expiry or missing shared-object data proves brittle, use iOS Shortcuts arrive/leave webhooks for the actual presence trigger and keep this client as an exploratory diagnostic. The follow-up HVAC actions should be separate: everyone away for a grace period powers off idle units; first arrival restores a saved profile. Lighting remains out of scope because this repo has no lighting backend.

## Presence webhooks, home location, and alarm automation

Presence automation uses iOS Shortcuts webhooks as the write path. Find My/iCloud is now only a bounded server-side diagnostic refresh for distance/status enrichment, so opening the Security tab every few seconds does not perform a fresh Apple locate call.

Set a shared webhook secret in `.env`:

| Key | Meaning |
|-----|---------|
| `PRESENCE_WEBHOOK_SECRET` | Secret required by the iOS Shortcuts webhook endpoints. |
| `PRESENCE_ICLOUD_REFRESH_ENABLED` | Optional, default `true`; set `0` to disable the cached Find My diagnostic refresher. |
| `PRESENCE_ICLOUD_REFRESH_INTERVAL_S` | Optional, default `900` (15 min); minimum 60 seconds between Find My diagnostic refreshes. |
| `PRESENCE_AUTOMATION_ENGINE_ENABLED` | Optional, default `true`; the persisted automation config still defaults off. |
| `PRESENCE_AUTOMATION_POLL_INTERVAL_S` | Optional, default `10`; how often the alarm consumer evaluates webhook-backed state. |

Shortcut endpoints:

```powershell
POST https://<host>:8447/api/presence/webhooks/roberto/home
Authorization: Bearer <PRESENCE_WEBHOOK_SECRET>
```

Use `home` for the Arrive automation and `away` for the Leave automation. Create one stable person id per phone, for example `roberto` and `ana`. The Security tab's Presence card can rename those ids, hide non-household Find My entities behind Show hidden, edit `config/location.json`, set the alarm automation thresholds, and toggle the **Kids home** override (see below). The automation defaults off and only acts on fresh webhook-backed people that are not hidden.

Set up each iPhone with two Personal Automations in Shortcuts:

1. **Arrive**: Shortcuts → Automation → New Automation → Arrive → choose the home geofence → Run Immediately → Get Contents of URL.
2. URL: `https://<host>:8447/api/presence/webhooks/<person_id>/home`; method: `POST`; header key: `Authorization`; header value: `Bearer <PRESENCE_WEBHOOK_SECRET>`. Do **not** use the dashboard `?token=` URL parameter here — the webhook uses its own secret.
3. **Leave**: duplicate the automation with URL `https://<host>:8447/api/presence/webhooks/<person_id>/away`.
4. Repeat with a different stable `<person_id>` for the other phone, for example `ana`.

To test immediately, run the same **Get Contents of URL** action from a temporary normal Shortcut (or tap the automation's run/play control if iOS shows one). A successful call returns JSON like `{"ok": true, "person_id": "ana", "state": "away"}`; the Security → Presence card then shows that person as `Shortcut · Person`. Opening the URL in Safari is not a valid test because Safari sends `GET` and the endpoint intentionally accepts only `POST`.

The browser-only **This device** row is diagnostic: it uses the browser Geolocation API and only updates while the dashboard tab/PWA is open. It is useful for setting/checking the home location, but it does not drive alarm automation. Find My/iCloud entries are also diagnostic enrichment; the reliable automation source is the Shortcut webhook state persisted in `config/presence_state.json`.

Home location is editable in the Presence card. The **Use this device location** button asks the browser for the current GPS position and writes it to `config/location.json`; the latitude/longitude fields can also be typed manually. Saving the location automatically re-runs the Find My diagnostics so distances recalculate from the new origin (the on-demand Refresh button was removed — the background refresher owns the cadence).

Alarm behavior:

- Everyone visible/confirmed away for the configured grace period → `control_system("arm")` (full), or `control_system("perimeter")` when the **Kids home** override is on.
- First fresh confirmed arrival while armed → `control_system("disarm")`.
- **Kids home** override (Presence card toggle): when active, the everyone-away trigger arms perimeter only instead of full — for leaving a child at home without arming the interior. It is transient: the next disarm-on-arrival auto-resets it to off, so the system never silently stays on perimeter when it should default to full. Stored in `config/presence_state.json`, not the persisted automation config.
- Stale/uncertain state never disarms.
- Any manual alarm action in the UI suppresses automation until a later presence transition.
- Each attempted transition appends a JSONL audit row to `logs/presence_triggers.jsonl` (gitignored).

### Family locator — "where's mom/dad" (#438)

A voice-queryable locator layered on top of the same presence data above — read-only, no new iCloud locate cost. Two pieces, both configured from the Security tab's Presence card:

- **Places** (Presence card → **Places**): named places (e.g. "the gym", "Roberto's work") with a radius, so the locator can say "Roberto is at the gym" instead of just a distance from home. Add one by typing coordinates, tapping **Use my current location**, or **Pick on map…** (an interactive Leaflet map — click/drag the pin, then confirm; the label auto-suggests via reverse-geocoding the picked point).
- **Role** (a person's detail modal → **Role**): an optional household-role alias (e.g. "dad", "mom") alongside their display name, so "where's dad" and "where's Roberto" resolve to the same person. Display name and role are edited together and persisted with an explicit **Save** button (issue #442) — matching every other rename modal in the app — rather than the old save-on-blur behavior.

`GET /api/presence` gains a `current_place` field per entity — the closest configured place within its radius, else "Home"/"Away" (from cached Find My coordinates for iCloud entities, or the webhook `home`/`away` state for Shortcut-backed people, which carry no coordinates). A collapsible **"Mom & Dad locator"** card on the Home tab shows this for every tracked (non-hidden) person **that has a role set** (issue #442) — an untagged Find My device or person doesn't clutter the card just for being tracked; a role is what makes it locatable by voice in the first place. Sourced from the same poll as the Presence card (no extra network cadence).

The voice bridge (`GET /api/presence/locate?who=<text>&lang=<en|es>`, resolved via role → display name → raw name, in that order) is wired as a Tier-1 deterministic Home Assistant command — see [`docs/voice-pe-config/README.md`](docs/voice-pe-config/README.md#family-locator-issue-438--wheres-momdad) for the installed sentence list and [`docs/voice-control.md`](docs/voice-control.md) for the architecture.

**Tolerant name matching + Spanish locate (issue #446).** Resolution folds accents, doubled letters ("Anna" ↔ "Ana"), and common kinship variants ("mum"/"mummy"/"mama"/"mamá" → the mom role; "daddy"/"papa"/"papá" → the dad role) — a deterministic variant table, not fuzzy matching, so Whisper's legitimate alternate spellings of a correctly-heard word resolve instead of failing on an exact string compare. The locator is also on the Spanish "Hey Jarvis" pipeline (`custom_sentences/es/locate.yaml` — "¿dónde está papá?", "localiza a Roberto"), which passes `lang=es` so the endpoint answers in Spanish ("Roberto está en casa"); since the Spanish Whisper hint hears "papá"/"mamá" unambiguously, this also sidesteps the English "dad"→"that" mishearing tracked in #444. Probe either language without speaking: `… -m scripts.ha_config_sync probe --text "donde esta papa" --language es --actuate`.

**Near-real-time locate + fail-loud on a broken source (issue #442).** `GET /api/presence` still only reads the shared background cache (no extra network cadence). `GET /api/presence/locate` is different: a locate query is user-initiated and rare, so when the cache is older than `PRESENCE_LOCATE_STALE_AFTER_S` (default `120`, or was never refreshed), it awaits one bounded on-demand refresh — capped at `PRESENCE_LOCATE_REFRESH_TIMEOUT_S` (default `5`) — before resolving, falling back to whatever is cached on timeout or failure. The background refresh cadence (`PRESENCE_ICLOUD_REFRESH_INTERVAL_S`) is unchanged either way. If the Find My source itself is down (iCloud diagnostics `reason` is `2fa_required`, `error`, or `not_configured`), a role/name known only through Find My answers with a distinct "location tracking needs re-authentication" speech instead of a generic "away — I don't know exactly where" or a raw error, and the Home-tab locator card shows the same note above the list. Genuinely "away, unknown exact location" (a real, current Find My read that just lacks coordinates) still gets the generic away message — only a broken *source* gets the re-auth wording.

A located person who just isn't within any configured **Place** no longer reads as a bare "Away" either: both the locator card and the voice speech reverse-geocode the coordinates (the same OpenStreetMap Nominatim lookup and cache `GET /api/location/reverse` already uses for the Presence card's address line) into something like "Ana is at Carrer Maria Benlliure, Barcelona." The card resolves this client-side (cached per rounded coordinate, same as the Presence card); the voice endpoint resolves it server-side against the same cache, bounded by Nominatim's own request timeout, and falls back to the generic "away" wording if the lookup is unavailable.

### Web Push setup

Web Push is browser-native push for the installed PWA. The app stores a browser subscription locally and uses VAPID keys to send a notification from the server when a presence transition fires. There is no third-party notification account, but iOS requires the dashboard to be opened as an installed PWA from the home screen before push subscriptions work.

Generate local VAPID keys:

```powershell
& .\.venv\Scripts\python.exe scripts\gen_web_push_keys.py mailto:you@example.com
```

Restart the webapp, open the installed PWA over HTTPS, go to Security → Presence, and tap **Enable notifications**. The private key and subscriptions live in gitignored `config/push_config.json` and `config/push_subscriptions.json`; the private key is stored as the base64url-encoded raw VAPID scalar (not a PEM string — a PEM-armored key fails ASN.1 parsing in `pywebpush`/`py_vapid`, see #284), and an unreadable key logs a single `Web Push private key unreadable — pushes disabled` warning at startup instead of a per-send error. Push delivery is best-effort; a failed notification never blocks arm/disarm. Re-running `gen_web_push_keys.py` rotates the keypair, so any existing browser subscriptions will need to re-subscribe (tap **Enable notifications** again) to pick up the new public key.

## Home-network / Network tab

Issue #125 added a read-only spike for the **Network** view; issue #129 builds the tab itself, in phases. **Phase 1 (live now):** the **📶 Network** tab sits between Plugs and Security and shows internet/WiFi/LAN health, the attached-device inventory grouped by band (weakest signal first), network-quality alerts, AP/router health, and the confirm-gated **Reboot AP** — so the network can be watched and managed without logging into the vendor web UIs by hand. The router-reboot button is present but disabled until Phase 3 wires the router data-read. **Phase 2 (live now):** device identity + rename — each device row shows a category glyph and a friendly label (custom name → OUI vendor → reported hostname → MAC), and tapping it opens a detail modal (vendor, IP, band, signal, SSID, MAC) with a rename field. Vendor comes from a bundled, trimmed OUI table (offline, slightly stale — an unknown prefix just falls back to the hostname/MAC); modern phones rotate a randomised MAC per network, so those are flagged and left un-vendored. **Phase 3 (live now):** the router card fills in with the live **WAN/internet status** (WAN up/down, public IP, default gateway, DNS, connection name, link uptime) read from the ZTE web API, and the **Reboot** button is enabled (confirm-gated, ~5-min outage). **Phase 4 (live now):** device **history & smart alerts** — a per-MAC registry (`network_history.py`) remembers each device, so the list shows **online/offline** state with "last seen Xh ago" on absent devices, a **Show offline** toggle (persisted, only shown when there are offline devices), a **new** badge for devices first seen in the last 24 h, and a **Mark important** switch in the detail modal. Two derived alerts join the strip: a never-before-seen device joining, and an *important* device dropping offline. **Network cleanup (live now):** attached devices also have a persisted **Hidden** switch in the detail modal; hidden devices stay out of the default list, show as dimmed when the header **Show hidden** pill is active, and remain separate from delete/history pruning. **Wi-Fi diagnostics (live now):** a collapsible tile reads the dashboard PC's own Windows Wi-Fi interface (`netsh wlan`) and shows the current SSID/signal/channel plus visible BSSIDs, signal %, band/channel, emitter MAC, and separate 2.4/5 GHz channel-overlap charts in the same translucent line/area style as the solar charts. Tapping a Wi-Fi row opens a detail modal with custom label, original SSID/BSSID, security/channel/signal, and a persisted **Hidden** switch; labels and chart tooltips use the custom name while preserving raw identity in the detail view. Randomised MACs aren't tracked (a rotating address isn't a stable device, and tracking it would spam the new-device alert). **DHCP hostname enrichment (live now, #169):** the AP often reports a client's name as `n/a`, so the router's DHCP allocated-address table is merged in by MAC to fill those blanks (e.g. `SMA1930031140`, `energymeter1900241927`, the Amazon/Elgato/Tuya hostnames) — far fewer devices read as "unknown". Wired clients the AP can't see at all are added from the router table too. Which source reported a device (access point, router DHCP, or both) shows as a **Seen by** row in the detail modal, not in the list. The router read is best-effort: if it fails, the AP inventory is unchanged. Full findings and the follow-up checklist are in [`docs/network-spike.md`](docs/network-spike.md).

- **Endpoints:** `GET /api/network` (one snapshot — `internet` / `access_point` / `router` / `wifi` / `devices` / `alerts`; the `wifi` block is best-effort and carries `available`, current interface/SSID/BSSID/signal/channel/band, visible BSSIDs, custom `display_name`, raw `original_name`, stable `wifi_id`, `hidden`, read-only recommendations, and structured `insights` with channel scores/rationale for later channel-management automation; the `router` block carries `wan_online` / `public_ip` / `gateway` / `dns` / `connection_name` / `uptime_s`; per device the merged `display_name`, `hidden`, OUI `vendor`, `category`, `randomized` flag, the `source` that reported it (`ap` / `router` / `both`), plus the Phase-4 history fields `online` / `first_seen` / `last_seen` / `times_seen` / `important` / `is_new`, and synthesised `online:false` rows for known-but-absent devices; an unreachable AP, router, or Wi-Fi scan is reported on its card/block, not an error), `GET /api/network?speedtest=1` (adds an opt-in `speedtest-cli` throughput run, ~13 s — never auto-run), `POST /api/network/access-point/reboot` (reboots the R9000; styled confirm, all clients drop ~1–2 min), `POST /api/network/router/reboot` (reboots the F6600P; styled confirm, ~5-min full outage), `PUT /api/network/devices/{mac}/display_name` (set/clear a device's custom label, persisted to gitignored `config/network_display_names.json` keyed by normalised MAC), `PUT /api/network/devices/{mac}/hidden` (hide/restore an attached device, persisted to gitignored `config/network_hidden.json`), `POST /api/network/devices/{mac}/important` (mark/unmark a device important — the flag lives in the history registry and an important device is never auto-pruned), `PUT /api/network/wifi/display_name` (set/clear a Wi-Fi label, persisted to gitignored `config/network_wifi_display_names.json` keyed by BSSID or explicit `SSID:<name>` fallback), and `PUT /api/network/wifi/hidden` (hide/restore a Wi-Fi row, persisted to gitignored `config/network_wifi_hidden.json`).
- **Cadence:** the tab refreshes every ~15 s **only while it is open** (the AP SOAP read + router WAN read are comparatively expensive) and stops polling when you leave it; the speed test is an explicit button, never on the poll.

It reads three independent sources: the **NETGEAR R9000 access point** over the Netgear SOAP API (`pynetgear`) for the device inventory + AP health + reboot; the **Vodafone ZXHN F6600P router** (ZTE) over its SHA256 web login, with authenticated WAN-status reads and reboot riding the per-request session-token + RSA integrity (`Check`) scheme its web UI uses; and **internet health host-side** (OS ping latency/packet-loss + an optional `speedtest-cli` throughput run). The AP runs in access-point mode and reports the whole wired+wireless LAN, so it carries the inventory; the router's DHCP allocated-address table is then merged in by MAC (#169) to fill missing hostnames and add any wired clients the AP can't see.

Config in `.env`:

| Key | Meaning |
|-----|---------|
| `NETWORK_AP_HOST` | Access-point LAN IP. |
| `NETWORK_AP_USERNAME` | AP web-admin user (usually `admin`). |
| `NETWORK_AP_PASSWORD` | AP web-admin password. |
| `NETWORK_AP_MAC` | *(Optional)* Stable MAC of the access point. When set and `NETWORK_AP_HOST` becomes stale after a DHCP change, the app looks up this MAC in the router's lease table and retries the connection at the discovered IP. Two options for handling a drifting AP IP: (1) **Recommended — DHCP reservation:** reserve the R9000's MAC → IP in the router's *Static Binding* form so the address never changes. (2) **Automatic rediscovery:** set `NETWORK_AP_MAC` and the app recovers without any `.env` edit — on a failed AP read it probes the discovered IP, updates its in-memory target on success, and degrades exactly as today if no candidate is found. |
| `NETWORK_ROUTER_HOST` | Router (gateway) LAN IP. |
| `NETWORK_ROUTER_USERNAME` | Router web user (usually `user`). |
| `NETWORK_ROUTER_PASSWORD` | Router web password. |

Run the smoke command:

```powershell
& .\.venv\Scripts\python.exe -m src.list_network              # health + inventory
& .\.venv\Scripts\python.exe -m src.list_network --speedtest  # + WAN throughput (~15 s)
& .\.venv\Scripts\python.exe -m src.list_network --reboot-ap   # reboot the AP (drops WiFi ~1-2 min)
& .\.venv\Scripts\python.exe -m src.list_dhcp_plan            # categorised DHCP reservation plan (#170)
& .\.venv\Scripts\python.exe -m src.list_dhcp_plan --apply    # push the create/change rows to the router (#176)
```

The CLI prints internet health (up/down, latency, packet loss, optional speed), AP and router health, host-PC Wi-Fi diagnostics, and the attached-device list (MAC, IP, band, signal %, name) with weak-signal/offline alerts. Do not commit device credentials, the WiFi SSID/password, visible SSID/BSSID scan dumps, LAN IPs, or MAC/device dumps; this repository is public.

### DHCP reservation plan (#170)

The F6600P hands out pool addresses in arbitrary order, so a device drifts across IPs over time and the LAN is hard to read at a glance. The **reservation planner** computes a tidy, permanent MAC→IP assignment grouped by category — e.g. `2–10` infrastructure, `11–20` phones/tablets, `21–30` cameras, `31–40` plugs, `41–50` lights — so an IP tells you what a device is.

Configure it by copying `config/dhcp_plan.sample.json` → `config/dhcp_plan.json` (gitignored — it would expose your device inventory) and editing the `ranges` (ordered category windows), `rules` (keyword → category, matched against the device's display-name/hostname/vendor), and `overrides` (manual per-MAC escape hatch). Without the file the planner reports every device as *unassigned* with a warning rather than failing.

Each device gets the **lowest free IP in its category range** — skipping any IP already reserved on the router, **including reservations held by offline devices** (so the planner never suggests an address that's already taken); a device already correctly placed keeps its IP (minimises churn); range overflow, unclassified devices, overlapping ranges, and randomised (un-reservable) MACs surface as explicit warnings. The plan folds in the router's **existing static bindings** and tags every row `reserved` (already bound to its planned IP), `create` (no binding yet), or `change` (bound to a different IP). Surfaced as the `src.list_dhcp_plan` CLI and the **DHCP reservation plan** section in the Network tab (`GET /api/network/dhcp-plan`, computed on open/refresh).

**Applying it to the router (#176).** Pushing the plan to the F6600P's static *DHCP Binding* table is an **opt-in, confirm-gated write to the live gateway** — never automatic, never on a poll. The Network tab's **Apply plan** button (and `src.list_dhcp_plan --apply`, and `POST /api/network/dhcp-plan/apply`) writes only the `create`/`change` rows, one at a time, leaving already-reserved rows untouched, and reports a per-row result so one rejected row never silently drops the rest. Devices pick up their reserved address on their next lease renewal. You can still apply rows by hand in the router UI instead — the plan is a copy-ready list either way.

> **The F6600P caps its static binding table at 10 reservations.** This is a firmware limit (the 11th create returns *"the number of entries has reached the maximum limit"*), so a LAN with more than ten devices cannot reserve them all — you choose which ten matter. The plan reads the table once and shows the slot budget up front (`Router holds 10 reservations · N slot(s) free`) plus a warning when the planned reservations overflow the free slots; **Apply** writes only what fits, skips the rest with a clear *"table is full"* note (it does **not** keep hammering the router), and a *change* row — which re-writes a slot the device already owns — still applies even when the table is full.

**Managing the ten slots from the app (#176).** Because the cap is real, the DHCP card is a small **staged reservation manager**: you build up a set of changes, then apply them all at once. Every write stays opt-in and confirm-gated, never on a poll.
> - **On the router now · N/10** lists every existing reservation, **including ones whose device is offline** (the rows you can't see in the live inventory). Tap the trash toggle to **stage a removal** (tap again to undo) — removing frees a slot.
> - **Suggested to add** shows only the rows that need a write (`New`/`Change`); tick the ones you want (an *all/none* shortcut sits in the header). **Unassigned** devices get a category dropdown (assign → it becomes a suggestion and is auto-ticked); **Randomised** private-MAC devices are listed separately as *not reservable* (the only fix is to turn off Private Wi-Fi Address on the device).
> - **Add a reservation manually** stages a `{mac, ip, name?}` row (shown as a chip) for a device the rules can't place or one not in the inventory at all.
> - **Apply changes (remove R · add A)** runs the whole batch in one router session — **removals first** (freeing slots), then adds (cap-aware) — with a live *After: U/10 used* budget so you can see before applying whether it fits. One endpoint does it: `POST /api/network/dhcp-reservations/apply` `{remove:[inst_id…], add_macs:[mac…], add_manual:[{mac,ip,name?}…]}`; plan IPs are recomputed server-side, never trusted from the client. Per-MAC group choices persist in gitignored `config/dhcp_overrides.json` (folded over `config/dhcp_plan.json` so a UI choice wins without editing the committed config). The binding read retries once, so the manager no longer degrades to a misleading "all applied" when the router briefly loses the login race with the 15 s poll.

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
> While history is younger than a window's nominal length (e.g. a few weeks old,
> vs. `month`'s 30 days or `year`'s 365), that window's fixed standing charge is
> prorated over the *actual* retained span rather than the full nominal one —
> otherwise `year` would charge a full year of fixed cost against a few weeks of
> real consumption. Expect `month`/`year`/`total` to show identical consumption,
> cost, and fixed-charge figures until history genuinely exceeds 30/365 days.

## Activity log (events & telemetry)

The **Home tab** has an **Activity log** button (not a tab — an admin/telemetry overlay) that shows the unified, filterable history of everything the home does: alarm arm/disarm, plug toggles, UPS power lost/restored, presence transitions, and RISCO panel events. It is the read surface over `src/telemetry.py`'s `events` store — the queryable successor to the previously write-only `logs/*.jsonl` trail and the otherwise-ephemeral RISCO event feed.

Events are recorded by their producers automatically: the shared `src/activity_log.append_activity` writer mirrors every alarm/power/presence event into the store, plug toggles are recorded in the Tuya router, and `GET /api/security/events` persists the live RISCO feed (deduped, so re-polling and restarts never double-insert). The overlay reads `GET /api/activity?domain=&type=&since=&limit=`, filtering **server-side** via the domain dropdown and the type box (a **substring** match — typing `w` finds `power_w`). Rows are labelled with the device's rename ("pc despacho", "hab. Luca") rather than a raw id, and a **"What do these mean?"** legend inside the overlay explains every event type, reading metric, and the severity colours. Readings with no value at all (an offline or non-metering device) are **not** stored — the device simply shows a gap, so the log isn't flooded with empty `—` rows.

The overlay also has a **Readings** view (toggle at the top): periodic device telemetry — HVAC room/set temperatures, plug watts/volts/amps/kWh, UPS load/charge/runtime, Elgato light state — captured by a background **telemetry sampler** (`app/webapp/telemetry_sampler.py`), a sibling of the energy sampler owned by the same webapp lifecycle. It runs on a gentle cadence (default 5 min — temperature/load trends don't need 60 s resolution) and isolates failures per domain, so one offline plug never stops the rest. Energy stays in its own `energy_history` store; presence is event-driven, so its reading gate defaults off.

The store lives at `webapp/telemetry.sqlite3` (gitignored, per-machine runtime, WAL mode, modeled on `energy_history.py`). Config + retention knobs (`.env`, all optional):

| Variable | Default | Meaning |
| --- | --- | --- |
| `TELEMETRY_SAMPLER_ENABLED` | `true` | Master switch for the reading sampler. `false`/`0` serves events + existing readings but captures no new readings (used by the e2e suite and dev runs). |
| `TELEMETRY_SAMPLE_INTERVAL_S` | `300` | Seconds between reading snapshots. |
| `TELEMETRY_SAMPLE_HVAC` / `_PLUGS` / `_UPS` / `_LIGHTS` | `true` | Per-domain gates — turn a flaky/slow domain off without disabling the rest. |
| `TELEMETRY_SAMPLE_PRESENCE` | `false` | Presence is captured as events; off by default to avoid redundant rows. |
| `TELEMETRY_READINGS_RETENTION_DAYS` | `7` | How long raw device readings are kept. |
| `TELEMETRY_EVENTS_RETENTION_DAYS` | `400` | How long discrete events are kept — far longer than readings, since events are rare and human-meaningful. |

> **Extensible by design (#283):** the `readings`/`events` tables are narrow (one row per observation/event) with a JSON sidecar column, so a new device type or field becomes new *rows*, never an `ALTER TABLE`. A new domain just needs a small pure adapter in `src/telemetry_adapters.py` (mapping its reading object → rows) wired into the sampler.

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

The PWA's **Plugs** tab is a Smart-Life-style control surface for these local Tuya devices, split into **two collapsible cards — Plugs and Blinds — both collapsed by default** (issue #191). Each renders its devices as a compact divider-separated **row list** in the same low-chrome style as the Network tab's "Attached devices" list, not chunky sub-cards. A **plug row** is a single **name · wattage · on/off** line (**live wattage on metered plugs**, so solar/load decisions are obvious without opening the vendor app); a **blind row** is **name + up / stop / down icon buttons** wired to the cover open/stop/close path. A **summary block** above the cards totals devices, switches on, switches off, and live consumption (summed across reachable metered plugs). It is **cloud-free at runtime** — it reads `devices.json` plus local LAN status only.

The tab also shows the local **UPS** above the Tuya device summary, rendered as the **same compact one-line tile as the Home tab** (identity · charge% · runtime · status pill). `GET /api/ups` reads the USB-connected APC Smart-UPS through NUT when the local `upsc` server is available, falls back to a one-shot NUT USB-HID probe, then falls back to Windows battery telemetry. The current APC SMT1000IC USB-HID path reliably exposes status, charge, runtime, battery voltage, model, manufacturer, and serial; it does **not** expose `ups.load`, `input.voltage`, or `output.voltage` through NUT on this machine.

**Power notifications (Telegram) + low-battery auto-shutdown.** The Plugs tab has a folded-by-default **Notifications** card (same structure as the Alarm tab's) with three toggles — **Mains power lost**, **Power restored**, and **Auto-shutdown PC when UPS runtime < 15 min (safety)** — all default **on**. The first two push a Telegram message when the UPS crosses between mains and battery. The third is a safety net: persisted to gitignored `config/power_notify_prefs.json` (`…sample.json` committed) via `GET`/`PUT /api/ups/notify-prefs`. Because the browser tile only polls while the Plugs tab is open, reliable alerts need a server-side watcher: a background **power monitor** (`app/webapp/power_monitor.py`, started in the webapp lifespan) reads the UPS every `POWER_MONITOR_POLL_INTERVAL_S` (default 60 s) in a worker thread and fires edge-triggered on the `mains_online` transition. Set `POWER_MONITOR_ENABLED=0` to disable it. Uses the same `src/notify/` Telegram credentials as the alarm alerts; transitions are recorded to gitignored `logs/power.jsonl` via the shared activity log.

When the UPS is on battery and its reported runtime drops to **15 minutes or less** (hardcoded, not user-configurable), the auto-shutdown toggle — if on — fires a distinct "shutting down now" Telegram alert and then schedules a graceful Windows shutdown via `src/host_shutdown.py` (`shutdown /s /t 180 /c "…"`): a 180-second grace window so open applications get a chance to autosave/close via `WM_QUERYENDSESSION` before Windows forces the shutdown at the deadline. This fires once per outage (edge-triggered in `power_monitor.py`'s process-memory state) and, unlike the mains-lost/restored alerts, is **not** suppressed on the monitor's first observation — if the webapp restarts while the UPS is already critically low, it still triggers, since this is a safety measure rather than a spam-avoidance one. If mains power returns before the scheduled shutdown completes, the pending shutdown is cancelled (`shutdown /a`) and the trigger resets for the next outage. The shutdown call is hard-blocked under `pytest` (mirrors the Telegram notifier's pytest guard in `src/notify_config.py`) so the test suite can never trigger a real shutdown.

> **Blind position is up/stop/close only.** The blind (Maxcio "Curtain switch") exposes a single open/stop/close DPS with **no native position % and no feedback** (confirmed by a live `tinytuya` DPS dump). Percentage **presets** would therefore need a time-based approximation (calibrated travel-time + a timed stop) and are **deferred to #181** along with group multi-blind control — this tab is the presentation refactor only.

- **Rename a socket:** tap a plug's name to open its detail modal (same UX as the AC-unit rename). The custom label is saved via `PUT /api/tuya/{id}/display_name` to a gitignored `config/tuya_display_names.json` (`device_id` → label, parallel to the unit `config/display_names.json`); a missing file is not an error. The override wins over the Tuya device name everywhere in the UI. The modal also shows the **original Smart Life name** (so a renamed device can be matched back to the app) and a **Hidden** toggle — the display name and Hidden edits commit together only on **Save** (closing discards them).
- **Hide a socket or blind:** the detail-modal **Hidden** toggle drops a device out of both lists; a **"Show hidden (N)"** toolbar toggle reveals them. Hidden state persists via `PUT /api/tuya/{id}/hidden` to a gitignored `config/tuya_hidden.json` (`device_id` → hidden marker, parallel to `config/network_hidden.json` / `config/security_hidden.json`); `GET /api/tuya` overlays the `hidden` flag per card.
- **Refresh local status:** tap **Refresh** to run a live LAN rediscovery — a TinyTuya UDP broadcast scan (no Tuya Cloud, no local keys) that finds the **current** IP of every powered-on plug, reconciles those addresses into `devices.json` by device id (local keys and DPS mappings preserved), then retries the LAN reads. This makes Refresh **self-healing for DHCP churn**: a plug that took a new lease becomes controllable again without leaving the app. The scan takes ~8 s, so it is gated to this explicit button (page-load reads stay fast off the stored file); the button shows **Scanning…** while it runs and reports what it recovered.
- **Endpoints:** `GET /api/tuya` (device cards with switch state, reachability, live energy fields, the `display_name` override, and the `hidden` flag — the per-device LAN reads run in parallel), `POST /api/tuya/{id}/switch` (`{"on": true|false}`), `POST /api/tuya/{id}/cover` (`{"action": "open"|"close"|"stop"}`), `PUT /api/tuya/{id}/display_name` (`{"display_name": "…"}`; empty clears the override), `PUT /api/tuya/{id}/hidden` (`{"hidden": true|false}`).
- **Cadence:** the tab refreshes every ~15 s **only while it is open** (LAN reads are comparatively expensive), and stops polling when you leave it.
- **Offline / stale-IP devices:** a powered-off plug or one without a usable LAN IP renders as **Unavailable** without blocking reachable devices from updating or being controlled. No-IP devices are visible by default with the captured non-secret identity fields (MAC/UUID/SN when TinyTuya provides them); use **Reachable only** to temporarily filter them out.
- **Missing or stale devices?** Stale **IPs** now self-heal — tap **Refresh** (above). The TinyTuya wizard/snapshot is only needed **on the home network** to add **new** devices or capture **updated local keys** (re-pairing), never just to chase a changed IP, and never the cloud:

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
custom label in gitignored `config/elgato_display_names.json`. Labels are keyed
by reported MAC address when available, so they survive DHCP/IP changes; older
host:port labels still load as a fallback until the next save migrates them.
The detail modal shows the original Elgato identity, LAN address, MAC metadata
when available, firmware, and colour-temperature readback. Spike findings and
the implementation choice are recorded in [`docs/elgato-lights.md`](docs/elgato-lights.md).

Optional config in `.env`:

| Key | Meaning |
|-----|---------|
| `ELGATO_LIGHT_HOSTS` | Optional comma-separated `host[:port]` list. Leave blank to try mDNS discovery only. The default port is `9123`. |

Endpoints:

- `GET /api/lights` — list Elgato lights with reachability, display-name
  override, durable `display_key`, original name, product, firmware, host/port,
  optional MAC metadata, power, brightness, and color temperature.
- `POST /api/lights/refresh` — re-run mDNS/configured-host discovery and retry
  each light endpoint. If a refresh fails while previous cards are still
  rendered, the tab keeps those cards and reports the failure as partial/stale
  data rather than saying no lights exist.
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
is a row showing a **last-snapshot thumbnail** (icon + name, aligned under the
card title), its model, and reachability; tapping the thumbnail **zooms** the
last frame, and tapping the name opens a detail modal with a **fresh snapshot**
(grabbed at open time, which *becomes* the new persisted last frame) and a rename
field, plus an **Open live view** button. The full-screen live view streams
MJPEG, with a **PTZ d-pad** that toggles between **Step** (one click = one fixed
nudge — precise, the default) and **Hold** (press-and-hold continuous move),
**saved position presets** (Position 1, 2, … — recall/save/delete), **manual
pan/tilt/zoom coordinate** entry, a **screenshot** button (downloads a still),
and a **record** toggle (server-side mp4). The precise-PTZ controls are
**capability-gated**: presets and absolute-coordinate entry appear only on
cameras whose ONVIF stack supports them; the universal step-nudge works on any
PTZ camera. Cameras are accessed the same vendor-neutral way the rest of the
fleet is: ONVIF for discovery/profiles/PTZ, RTSP + **ffmpeg** for the
snapshot/stream/recording. The eventual goal is alarm-triggered scene capture
with AI analysis.

- **Config:** declare cameras in gitignored `config/cameras.json` — copy
  `config/cameras.sample.json` and fill in each camera's `id`, `host`,
  `onvif_port` (Reolink default 8000), `rtsp_port` (554), `username`, and
  `password` (the on-device **device account**, NOT the cloud login). Custom
  labels persist to gitignored `config/camera_display_names.json`. Position
  presets on cameras **without** native ONVIF presets fall back to
  gitignored `config/camera_presets.json` (absolute coordinates); the most
  recent frame per camera is persisted under gitignored
  `webapp/camera_captures/last/`.
- **Address recovery model:** give a camera an optional **`mac`** in
  `config/cameras.json` and a stale DHCP IP **self-heals** like the plugs and
  access point do (#190). When the configured host is unreachable, the app
  looks the MAC up in the access-point's attached-device table
  (`network_client.resolve_ip_by_mac`), reconnects at the rediscovered IP, and
  **persists the recovered address back to `config/cameras.json`**. Without a
  `mac` (or when the MAC isn't on the network), the camera is simply marked
  unreachable — recovery is best-effort and never fatal.
- **Prerequisite:** **enable RTSP + ONVIF on the camera first** — Reolink ships
  them off (app: Settings → Network → Advanced → Server Settings).
- **Endpoints:** `GET /api/cameras` (list; each carries `ptz_presets` /
  `ptz_absolute` / `ptz_relative` capability flags), `GET /api/cameras/{id}/snapshot`
  (fresh JPEG, also persisted as the last frame), `GET /api/cameras/{id}/last_snapshot`
  (the persisted last frame — never hits the camera), `GET /api/cameras/{id}/stream`
  (live MJPEG; reachable from the PWA via `?token=`), `POST /api/cameras/{id}/ptz`
  (`{action: start|stop|step, direction, zoom}`), `GET /api/cameras/{id}/ptz/status`
  (live pan/tilt/zoom + bounds), `POST /api/cameras/{id}/ptz/absolute`
  (`{pan, tilt, zoom?}`), `GET/POST /api/cameras/{id}/presets` (list / save
  current), `POST /api/cameras/{id}/presets/{token}/goto`,
  `DELETE /api/cameras/{id}/presets/{token}`, `POST /api/cameras/{id}/record`
  (`{action: start|stop}` → mp4 in gitignored `webapp/camera_captures/`),
  `PUT /api/cameras/{id}/display_name`.
- **Needs ffmpeg on PATH.** Smoke command:

  ```powershell
  & .\.venv\Scripts\python.exe -m src.list_cameras
  ```

Do not commit camera IPs, the device-account password, the UID/MAC, captured
frames, or location names; this repository is public.

## Alarm scene capture + AI verdict (#162)

When the RISCO alarm trips, the detector that fired is matched to its configured
camera **pairings**; each paired camera is driven to its PTZ preset and
snapshotted, and the frames (plus each camera's most recent *calm* baseline, for
contrast) are sent to a vision LLM that returns a **real vs false alarm** verdict
with a short explanation (person / pet / vehicle / nothing moved). The verdict is
delivered over **Web Push and Telegram** — the Telegram message **attaches the
captured frame** — and every trigger + the full model reply is appended to the
gitignored `logs/alarm_scene.jsonl` audit log.

Only detectors you **pair** are photographed — a random detector firing never
captures the house. A tripped detector with no pairing is logged and skipped. The
feature rides the single RISCO read already done by the presence-automation loop
(no second poller), so it requires that loop running
(`PRESENCE_AUTOMATION_ENGINE_ENABLED`, default on). While no alarm is active, each
camera's calm baseline is refreshed on a low-frequency timer.

**Onset detection reads RISCO's event log, not the live alarm flags** (issue
#325). RISCO's `ongoing_alarm`/`memory_alarm` system flags latch `True` until a
full disarm+dismiss cycle, so a *second* real alarm on the same detector within
one still-armed session never produces a fresh transition of those flags — and
the live per-zone `triggered` boolean can already be stale by the time a poll
observes it. Instead, while the system reports an active intrusion, the
automation periodically (`ALARM_SCENE_EVENT_SCAN_S`) diffs RISCO's event log
against a cursor — the timestamp of the last-processed alarm event, persisted
to gitignored `config/alarm_scene_cursor.json` rather than kept in memory — so
each individual alarm gets its own capture regardless of how many fired in the
same still-armed session, and a webapp restart mid-session resumes from the
persisted cursor instead of silently dropping an in-flight alarm.

The LLM call is routed through the **local hub** (`http://127.0.0.1:8000`,
Anthropic-shape `/v1/messages`) per the fleet rule — never an inline `claude -p`
wrapper. A hub or parse failure degrades to an "AI analysis unavailable — verify
manually" verdict and never breaks the alarm path.

**Configure pairings** in the **Security tab → Scene capture** card. Saved
pairings appear as compact detector-to-camera rows; tap a row or **Add pairing**
to edit the detector, camera, and optional PTZ preset in a dialog. Closing the
dialog without **Save** discards the change. Pairings can also be edited by hand in gitignored
`config/alarm_scene_pairings.json` (copy `config/alarm_scene_pairings.sample.json`;
each entry is `{zone_id, camera_id, preset_token?, preset_name?, enabled}`). API:
`GET/PUT /api/security/scene-pairings`.

Config in `.env`:

| Variable | Meaning |
| --- | --- |
| `ALARM_SCENE_ENABLED` | Optional, default `true`; set `0` to disable scene capture while keeping the pairing UI/API available. |
| `ALARM_SCENE_MODEL` | Optional, default `claude-haiku-4-5`; the vision model id the hub routes to. |
| `ALARM_SCENE_HUB_URL` | Optional, default `http://127.0.0.1:8000`; the local hub base URL. |
| `ALARM_SCENE_PRESET_SETTLE_MS` | Optional, default `4000`; how long to let a camera settle on its PTZ preset before the snapshot (a too-short wait captures a blurred mid-pan frame). |
| `ALARM_SCENE_BASELINE_REFRESH_S` | Optional, default `1800`; how often the calm per-camera baseline is refreshed while no alarm is active. |
| `ALARM_SCENE_EVENT_SCAN_S` | Optional, default `20` (floor `10`); how often the RISCO event log is diffed for new alarms while an intrusion is active. |

Captures + baselines live under the already-gitignored `webapp/camera_captures/`
(`alarm/` and `baselines/`); never commit frames or `config/alarm_scene_pairings.json`.

## Alarm override — auto-bypass after repeated false alarms (#341)

RISCO's own panel already auto-omits a repeatedly-triggered detector, but only
after an uncontrolled, undocumented number of repeats. The **Security tab →
Override** card (collapsed by default, right after Notifications) lets you set
a much tighter, per-detector threshold: after 1-3 real alarms from the same
detector within one armed session (a windy garden gate, a roaming cat), the app
proactively bypasses just that detector — the rest of the system stays fully
armed — and restores it automatically the next time the panel is armed.

Reuses the same event-log-diff architecture as alarm scene capture (#325)
rather than a second poller: it rides `presence_automation.py`'s single RISCO
read, diffing `fetch_events()` against a cursor + per-zone trigger counts
persisted to gitignored `config/security_override_session.json`. Each
auto-bypass/restore is logged to the unified activity log (`GET
/api/activity?domain=security`, `event_type` `auto_bypass` / `auto_unbypass`).

**Configure** in the **Security tab → Override** card. Saved rules appear as
compact detector + threshold rows; tap a row or **Add override** to edit the
detector and "bypass after N triggers" threshold in a dialog. Closing without
**Save** discards the change. Rules can also be edited by hand in gitignored
`config/security_override.json` (copy `config/security_override.sample.json`;
each entry is `{zone_id, max_retries, enabled}`, `max_retries` clamped to
1-3). API: `GET/PUT /api/security/overrides`.

Config in `.env`:

| Variable | Meaning |
| --- | --- |
| `SECURITY_OVERRIDE_ENABLED` | Optional, default `true`; set `0` to disable the automation while keeping the override UI/API available. |
| `SECURITY_OVERRIDE_EVENT_SCAN_S` | Optional, default `20` (floor `10`); how often the RISCO event log is diffed for new alarms/arm events. |

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
`webapp/certificates/cert.pem` exists (see [HTTPS](#https-tailscale-cert)),
otherwise plain HTTP. Invoke uvicorn directly if you prefer:

```powershell
& .\.venv\Scripts\python.exe -m uvicorn app.webapp.server:app --host 0.0.0.0 --port 8447 `
    --ssl-keyfile webapp/certificates/key.pem --ssl-certfile webapp/certificates/cert.pem
```

The signal that new code is live is the unit grid rendering (6 units).

## HTTPS (Tailscale cert)

The webapp is reached over Tailscale, so HTTPS uses a **real Let's Encrypt
certificate** issued for the tailnet MagicDNS name via `tailscale cert`. Every
device already on the tailnet trusts Let's Encrypt, so there are **no
per-device trust steps** — no CA to install, no iOS profile, no Chrome restart.

**One-time setup (per tailnet):** enable HTTPS in the Tailscale admin console —
[**DNS → HTTPS Certificates**](https://login.tailscale.com/admin/dns). Then
provision the cert (auto-detects this machine's MagicDNS name):

```powershell
& .\.venv\Scripts\python.exe scripts\gen_tailscale_cert.py
```

This writes `cert.pem` / `key.pem` into `webapp/certificates/`; restart the
webapp (`tray.bat --restart`, or `webapp.bat`) and open
`https://<pc>.<tailnet>.ts.net:8447`.

> **Auto-renew (no calendar reminder needed).** A Let's Encrypt leaf is valid
> ~90 days, so renewal is automated rather than manual: both boot paths run
> `gen_tailscale_cert.py --check` on startup, which re-issues the cert only
> when it is a `.ts.net` cert expiring within 30 days (a no-op otherwise). The
> tray-owned webapp self-heals on its next restart; `webapp.bat` does the same.

Plain desktop access on the PC itself uses `http://localhost:8447`
(`https://localhost:8447` would warn — the cert is for the `.ts.net` name, not
loopback). The tray's **🏠 Open** action opens the trusted `.ts.net` URL
directly, so the lock is green with no certificate fuss.

### Phone install (PWA)

The webapp installs to the iPhone/Android home screen as a full-screen app:
open `https://<pc>.<tailnet>.ts.net:8447` in Safari/Chrome — the lock is solid
on first visit thanks to the Let's Encrypt cert (see
[HTTPS](#https-tailscale-cert)) — then **Share → Add to Home Screen**.

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

**Known issue — night idle-jump correlates with musty smell + buzzing (#386).**
One unit intermittently produces a stale/musty (humidity-like) smell and a
buzzing noise at night that only stops once the setpoint is manually touched.
Likely not a control-logic defect: the idle jump above is by design (#114,
specifically to avoid power-cycling the compressor), and the symptoms line up
with mechanical/moisture behavior *during* that idle state — an inverter
compressor/EEV hunting at minimum modulation (buzz, which any setpoint change
resolves by forcing the valve to reposition) and a coil that isn't cold enough
to actively condense/drain while the fan still moves air across it (musty
smell). Recommended next step is a physical inspection of the unit's filter,
coil, and condensate drain, not a software power-cycle override — reversing
#114's tradeoff wouldn't address a wet coil and risks compressor wear for no
benefit. See #386 for the full investigation notes; close it once the
inspection outcome is known.

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

## Wake alarms & timers

Alexa-style **wake alarms** and countdown **timers**, managed from a collapsible card on the Home tab (after the Home Assistant tile, before the Activity log). This is deliberately **separate from the RISCO "Alarm controls"** card — a wake alarm rings/notifies at a set time, it never arms or disarms the security system (issue #304, Step 1/2; the voice-control wiring is Step 2/2, #306).

- **Wake alarms** — recurring (any set of weekdays) or one-shot (a specific date). Each has a label, a `HH:MM` time, and an Enabled toggle. When one comes due the tray-owned webapp marks it "ringing" (shown in the card + a best-effort Telegram notify via the existing notifier), then rearms a weekly alarm for its next matching day or auto-disables a one-shot. Evaluated server-side so they fire while the PWA is closed. Persisted to gitignored `config/wake_alarms.json` (committed `…sample.json` shows the shape).
- **Timers** — one-off countdowns started from the card (presets 5/10/15/30 min or a custom minute count). Deliberately **in-memory and unpersisted**, mirroring Home Assistant's own ephemeral voice-set timers — a webapp restart clears active timers.

API: `GET`/`PUT /api/wake-alarms` (list/replace), `POST /api/wake-alarms/{id}/test` (fire immediately) + `…/dismiss`; `GET`/`POST /api/wake-timers` and `DELETE /api/wake-timers/{id}`.

**Voice (Step 2/2, #306).** The household's HA Voice PE creates/cancels/lists wake alarms hands-free — *"Okay Nabu, set a wake alarm for 7 am on weekdays"* / *"cancel my wake alarm"* / *"what wake alarms do I have"* — via a thin voice API that parses the spoken time server-side: `POST /api/wake-alarms/voice` (`{phrase}` → parse, append, speak it back), `POST /api/wake-alarms/voice/cancel` (cancels the **soonest** upcoming one), `GET /api/wake-alarms/voice` (spoken summary). Parsing lives in `src/wake_alarms.py:parse_spoken_alarm` (tested): times like `7 am` / `7 30` / `seven thirty` / `half past six` / `noon`; schedules `on weekdays` · `on weekends` · `every day` · a weekday name · `tomorrow`/`today` (one-shot). The HA sentences/wiring are in [`docs/voice-pe-config/`](docs/voice-pe-config/) (`wake_alarm.yaml`), kept collision-free from the RISCO "alarm" grammar. **Countdown timers** need nothing here — HA's native *"set a timer for 5 minutes"* already works on the Voice PE.

Optional `.env` knobs:

| Key | Default | Meaning |
|-----|---------|---------|
| `WAKE_ALARMS_ENABLED` | `true` | Master switch. Set `false`/`0` to disable the evaluator while keeping the UI/API available. |
| `WAKE_ALARMS_POLL_INTERVAL_S` | `15` | How often the tray-owned webapp checks for due alarms and expired timers. |

> **Not** in scope here: mirroring **voice-set** HA-native timers into the webapp — HA exposes no stable poll API for those, so the app-native timer above is a separate, unbridged pool. Triggering an HVAC unit from an alarm is covered by the per-unit [HVAC schedules](#hvac-automation), not duplicated here.

## Voice control (hands-free, fully local)

A Home Assistant Voice PE puck driven by the local LLM hub gives hands-free, **no-cloud**
voice control. Common commands route **deterministically** — spoken phrase → HA local
sentence match → `intent_script` → `rest_command` → the app's HTTP API — so no LLM is on
the command path and a hallucinated reply can never actuate. The first live bridge is
**alarm control** (#88): *"Okay Nabu, perimeter on"* / *"disarm now"* / *"what's the alarm
status"* hit `POST /api/security/{arm,partial,perimeter,disarm}` and `GET /api/security`,
with a spoken-code gate on disarm. **Wake alarms** (#306) are the second bridge — see
[Wake alarms & timers](#wake-alarms--timers) above.

- **Operating & architecture manual:** [`docs/voice-control.md`](docs/voice-control.md)
  (setup, pipeline, troubleshooting, the live alarm action bridge).
- **Wiring more commands:** [`docs/voice-commands-howto.md`](docs/voice-commands-howto.md)
  — the reusable how-to (sentence syntax, the `intent_script` response gotcha,
  reload-vs-restart, code-gating destructive commands, testing without speaking). Start
  here for every new command.
- **Installed HA config (secret-free):** [`docs/voice-pe-config/`](docs/voice-pe-config/).

### Home card: Voice PE rooms + push-to-talk

The existing neutral **Home Assistant** VM tile after Energy is now one disclosure, collapsed by default. Its summary keeps the VM's live `online` / `off` / unavailable state; opening it reveals the VM power control plus every `assist_satellite` Home Assistant currently owns. Names and room assignments come from HA's entity/device/area registries—there is no duplicate rename store here. Each row shows room, satellite activity, companion media-player volume, and a 44 px microphone control.

Push-to-talk copies App Launcher's proven local path: the browser records with `MediaRecorder`, sends ordered one-second chunks to this app, and this app proxies Voice Transcriber's stable `POST /api/sessions` → `/chunk` → SSE `/events` → `/finish` contract. Rolling Whisper partials replace the visible text while you speak; Stop settles the canonical transcript and calls HA's `assist_satellite.announce` for that room. Sessions carry `source: home-automation`, so audio/transcript recovery and attribution stay in Voice Transcriber History. Voice Transcriber reuses the already-running `:8090` Whisper process (currently local-LLM-hub-owned); this app loads no model and retains no audio.

The webapp also ingests HA's bounded Assist debug traces while it is running. Every 15 seconds it lists the three pipelines, fetches only unseen completed runs, normalizes wake/pipeline, STT text, local-vs-fallback intent, action/target, spoken response, satellite, outcome, and timestamp, and stores only that compact event in `webapp/telemetry.sqlite3`. The dedupe set is capped at 256 IDs; no raw trace blob or audio is held. Detailed satellite polling runs only while Home is active and the disclosure is open. Logging is intentionally best-effort: interactions during webapp downtime are not guaranteed, as agreed for #239.

Runtime API: `GET /api/ha`; `POST /api/ha/satellites/{entity_id}/announce`; and the App Launcher-shaped transcription proxy under `/api/ha/transcribe*`. It reuses `HA_URL` / `HA_TOKEN`; `VOICE_TRANSCRIBER_URL` defaults to `https://127.0.0.1:8443`; `HA_TRACE_ENABLED=0` disables only background trace ingestion.

**Measured warm-path latency (2026-07-16).** A 4.24 s local speech sample, sent through the live HTTPS webapp in four browser-shaped chunks, produced its first rolling partial at **2.81 s**, returned the canonical transcript **1.33 s after Stop**, and had HA accept the room-specific `assist_satellite.announce` call **6.75 s later**. The final interval is HA's synchronous announce/TTS service time (the endpoint validates one entity state and does not reload registries); it measures HTTP acceptance, not when the puck finishes speaking. This supports the bounded push-to-talk interaction but is a **no-go for a Tier-3 always-open conversation mode**: continuous capture would add wake/VAD, interruption and full-duplex lifecycle work, idle microphone/privacy exposure, and sustained resource use without removing HA's dominant response delay. No Tier-3 follow-up is opened for #239.

### Home Assistant config deploy over SSH

`scripts/ha_config_sync.py` (#243) deploys the repo-owned voice-PE config into the HA VM's `/config` over SSH, so HA config work is code-driven instead of done in the browser File editor: edit → deploy → `ha core check` → reload/restart → text-probe. It pushes the managed block in `configuration.yaml` and every `custom_sentences/en/*.yaml` file (`alarm.yaml` + `wake_alarm.yaml`), takes a timestamped backup under `/config/backups/home-automation/` before every write, applies a sentences-only change with the narrow `conversation.reload`, and guards the full restart a `configuration.yaml` change needs behind `--restart`. Real HA secrets stay live-only on the VM — the script verifies the required secret **key names** exist in `/config/secrets.yaml` but never reads, prints, copies, or commits their values.

```powershell
& .\.venv\Scripts\python.exe -m scripts.ha_config_sync preflight        # readiness; distinct failure per mode
& .\.venv\Scripts\python.exe -m scripts.ha_config_sync deploy --dry-run # unified diff, writes nothing
& .\.venv\Scripts\python.exe -m scripts.ha_config_sync deploy           # backup + write + ha core check
& .\.venv\Scripts\python.exe -m scripts.ha_config_sync deploy --restart # + the full HA restart a config change needs
& .\.venv\Scripts\python.exe -m scripts.ha_config_sync rollback         # restore most recent backup + recheck
& .\.venv\Scripts\python.exe -m scripts.ha_config_sync probe            # read-only "what is the alarm status" probe
```

**One-time bootstrap** (the SSH channel is the **Terminal & SSH add-on**, which mounts `/config`; HAOS host SSH on `:22222` is break-glass and not used): in the add-on's Configuration, paste this PC's public key into `authorized_keys` and expose a LAN-only host port (e.g. `2222`); create a long-lived access token; put the host/port/key/url/token into `.env` (`HA_SSH_*`, `HA_URL`, `HA_TOKEN` — see `.env.example`); then run `preflight`. Full bootstrap steps: [`docs/voice-pe-config/README.md`](docs/voice-pe-config/README.md).

## Home Assistant Hyper-V VM (status + control)

Home Assistant runs as a **Hyper-V VM** on this PC (TOWER), bridged onto the LAN so it has its own MAC and DHCP-reserved IP. The **Home Assistant disclosure after Energy** shows that VM at a glance while folded — `online · up 3d 4h` (or `off`) — and exposes the existing Start/Stop control when opened alongside the Voice PE rooms above. `GET /api/hyperv` reads the state; `POST /api/hyperv/{start|stop}` controls it. The VM is addressed **by name** (`HA_VM_NAME`), so the card is independent of its IP, and the backend only ever touches that one VM — never the host's other VMs (e.g. WSL2's hidden utility VM). **Stop is a graceful ACPI shutdown** (confirm-gated in the UI); there is no hard power-off.

**`HA_VM_NAME`** (in `.env`) is the exact VM name as it appears in Hyper-V Manager / `Get-VM` — e.g. `Home Assistant`. Nothing is hardcoded.

**Rights prerequisite — "Hyper-V Administrators".** The webapp runs under the tray user, which by default **cannot** run `Start-VM` / `Stop-VM` (and often not even `Get-VM`). Grant VM lifecycle control **without** full machine admin by adding that account to the local **Hyper-V Administrators** group, then sign out/in (or restart the tray) so the new token takes effect:

```powershell
# Run once, elevated. Replace with the account the tray/webapp runs as.
Add-LocalGroupMember -Group "Hyper-V Administrators" -Member "$env:COMPUTERNAME\YourUser"
```

Until then the tile shows a distinct `⚠ insufficient Hyper-V rights` state (read and act can fail independently — the card surfaces each cause: insufficient rights · VM not found · already in that state).

### Static MAC + DHCP reservation (`.4`)

The VM is pinned to **`192.168.0.4`** via a **static MAC + router DHCP reservation** so a VM re-import (as happened during the #88 spike) can't churn its address or leave a stale lease. One-time, manual (host + router — the app only *displays* MAC/IP so you can verify it landed on the reserved address):

1. **Pin the MAC static** (elevated PowerShell). Reuse the VM's *current* dynamic MAC so no new lease is generated — read it, then set it static:
   ```powershell
   (Get-VMNetworkAdapter -VMName "Home Assistant").MacAddress      # e.g. 00155D012A0B
   Set-VMNetworkAdapter -VMName "Home Assistant" -StaticMacAddress "00155D012A0B"
   ```
2. **Reserve `.4` to that MAC** on the router (Vodafone ZXHN F6600P → DHCP / static binding), and **delete the stale old-MAC `homeassistant` entry** so only one reservation remains.
3. **Reboot the VM** so it picks up the `.4` lease, then verify: the Home tile's sub-line should read `192.168.0.4 · 00:15:5D:01:2A:0B`.
4. **Re-point the deploy config**: set `HA_SSH_HOST=192.168.0.4` and `HA_URL=http://192.168.0.4:8123` in `.env`, drop the stale SSH host key (`ssh-keygen -R "[192.168.0.102]:2222"`), and re-run `scripts/ha_config_sync.py preflight`.

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

Rendered-geometry design checks (44px effective touch targets, non-overlap,
horizontal overflow, live Chart.js tick/cue config) go through
`tests/e2e/_geometry.py` — vendored **byte-identical** from
`project-scaffolding/tests/e2e/_geometry.py` (like `app/tray/single_instance.py`;
never fork it, upstream changes first). `tests/e2e/test_design_matrix.py` runs
the helper's 320/390/430/772px × light/dark matrix against the home view,
driving the theme through the app's own `home-automation.theme` localStorage
boot path with a per-leg check that the theme actually applied.

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
