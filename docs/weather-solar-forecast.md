# Weather and solar forecast

## Weather

The **Home tab**'s page header carries the weather as its live line: the sky and temperature now, then today's min / max
("Clear 21° · 9° / 21°"), for the home location, read from **Open-Meteo**
(keyless — no account, no API key). The line's accessible name adds today's
forecast sky and the location label. The header shows no clock (the phone's status bar already does).

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
  quietly thereafter.
- **Cadence:** the frontend polls every ~10 minutes (weather barely moves).

## Solar forecast (expected generation)

The Energy tab's **Solar forecast** card shows an *expected generation* curve
(dashed) with the day's measured generation overlaid (filled), a headline
"Expected generation +X kWh", a caption with the array parameters the curve was
computed from (e.g. `7.9 kWp · 15° · S  +  0.9 kWp · 15° · N · PR 0.80` for a
two-orientation roof), and a **Yesterday / Today / Tomorrow** toggle. It is
read/visualisation only — a forecast to compare against reality, not a control
input.

- **Source:** one keyless **Open-Meteo** call *per sub-array* for hourly
  *global tilted irradiance* (the same host the weather tile uses; Open-Meteo
  doesn't batch tilt/azimuth, so a multi-orientation roof fires one concurrent
  request per orientation), scaled and summed into an expected-generation
  curve. Self-contained and approximate — see
  [`pv-forecast.md`](pv-forecast.md) for the model.
- **Config:** `config/pv_system.json` (gitignored) — one entry under `arrays`
  per physically-uniform sub-array (`kwp`, `tilt_deg`, `azimuth_deg` —
  Open-Meteo convention: 0 = South, −90 = East, 90 = West, always non-negative
  tilt), plus one shared `performance_ratio`. A single-orientation roof needs
  just one entry; the legacy flat single-orientation shape still
  loads unmigrated. Coordinates are reused from `config/location.json` (the
  weather tile's file) — there is no separate lat/lon.
- **Panel temperature — optional, off by default:** setting
  `thermal_model_enabled: true` adds a PVWatts-style cell-temperature derate
  (NOCT 45 °C, γ = −0.0035/°C), fetching `temperature_2m` in the *same*
  Open-Meteo request. Left off, nothing changes — not the numbers, not the
  payload keys, not even the request. Turning it on is a **migration**:
  `performance_ratio` stops meaning "system losses + a constant thermal
  allowance" (~0.80) and must become system-loss-only (~0.88), or the thermal
  loss is counted twice. The half-migrated pair is refused rather than computed
  — a 400 from the editor, `reason: "thermal_ratio_unmigrated"` from the
  forecast. See [`pv-forecast.md`](pv-forecast.md) → "Panel
  temperature".
- **Horizon / shading profile — optional, off by default:** a
  hand-entered obstruction-elevation-by-azimuth profile (`horizon_profile`,
  editable from the PV system card regardless of the switch) that, once
  `horizon_profile_enabled: true` is set, attenuates an hour's direct-beam
  share whenever the sun sits at or below the profile's elevation for its
  azimuth. Left off (the committed default — pending an installer conversation
  about the real obstruction geometry), nothing changes, same contract as the
  thermal term above. See [`pv-forecast.md`](pv-forecast.md) →
  "Horizon / shading profile".
- **Editing it:** the **PV system** card in Settings adds/edits/removes panel rows, horizon points, and the shared
  performance ratio and coordinates — no file editing needed, and no restart:
  the forecast reads the config per request. The file stays the source of
  truth and remains hand-editable (`config/pv_system.sample.json` is the
  committed template); a hand-written `_doc` note survives an edit made in the
  app. `GET`/`PUT /api/energy/pv-system`; the `PUT` validates strictly and
  returns 400 naming the offending field rather than clamping it.
- **Endpoint:** `GET /api/energy/forecast?day=yesterday|today|tomorrow` returns
  the hourly expected curve, the day's `expected_total_kwh`, the `system` params
  used (`arrays` / `total_kwp` / `performance_ratio`), and (for today/yesterday)
  the measured `actual` overlay (`null` for tomorrow). Each overlay point
  carries the untouched `measured_wh` alongside the plotted `wh`, its hour's
  `coverage`, and `estimated` — set when `wh` is a projection rather than a
  measurement, either because the hour is still in progress or because the feed
  only partly covered it; `actual_gap_hours` totals the latter for the day. When
  `pv_system.json`/`location.json` is absent or Open-Meteo is unreachable it
  returns `{available: false, reason}` with HTTP 200 — the card keeps a one-line
  note and nothing else breaks.
- **Caching & rate limits:** the upstream response is cached per
  `(location, array-set, day)` for 15 minutes and one `aiohttp.ClientSession`
  is reused across calls, so repeated card renders cost one upstream request
  set, not one per render. A `429` from Open-Meteo gets its own
  `reason: "rate_limited"` (distinct from `unreachable`) and opens a backoff
  (60 s, doubling to a 900 s ceiling); while backing off, the last good curve
  is served instead of a blank card as long as it's under 6 hours old. See
  [`pv-forecast.md`](pv-forecast.md) → "Caching, rate limits and
  backoff".

### Sun-position diagnostic (measured vs modelled)

In Settings › Energy, the **Sun-position check** sheet re-plots a
day's *measured* performance ratio against where the sun actually was, next to
the flat modelled ratio. It exists to answer one question — is the afternoon
drop-off shading or weather? — from data the app already has, without deriving
it by hand against sqlite. A dip that lands at the same **azimuth** every day is
fixed obstruction geometry; one that moves with the clock is not.

- **Read-only, and deliberately a sibling.** It changes nothing the forecast
  predicts, and `GET /api/energy/forecast` is untouched —
  the diagnostic lives at its own endpoint precisely so that stays true.
- **What it plots:** effective performance ratio per hour, `actual_Wh / (kWp ·
  GTI)`, which falls straight out of the modelled curve as `PR · actual_Wh /
  expected_Wh`. No second irradiance source, no second model.
- **Gaps are excluded, and said so.** An hour whose sample coverage is below
  75% (`MIN_TRUSTED_COVERAGE`, the signal for partial-coverage hours
  above) is dropped and counted in the note under the chart. Plotted raw, a
  morning the feed spent offline is a low performance ratio at a morning
  azimuth — indistinguishable from shade, and exactly the wrong conclusion.
- **Endpoint:** `GET /api/energy/sun-overlay?date=YYYY-MM-DD` (defaults to
  today) returns `points` — each with `azimuth_deg` / `elevation_deg` (NOAA
  convention: 0° = N, 90° = E, 180° = S), `effective_pr`, `actual_wh`,
  `expected_wh`, `gti_w_m2`, `coverage` — plus `excluded` (hour + reason:
  `coverage` / `no_data` / `in_progress`) and `modelled_pr`. A future or
  malformed date is a 400; a day with no rollup history is an *empty* overlay,
  not an error. Irradiance history reaches back ~92 days (Open-Meteo's
  `past_days` ceiling), hourly rollups 400 — beyond the former, `{available:
  false, reason: "too_old"}`.
- **Sun position** is computed in-process by `src/sun_position.py` (NOAA's
  low-precision algorithm, stdlib-only — no new dependency), from the same
  `config/location.json` coordinates the forecast and weather tile use.
