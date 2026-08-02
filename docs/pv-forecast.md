# Solar generation forecast — the model

The Energy tab's **Solar forecast** card (issue #39) overlays an *expected generation* curve on the day's measured generation, for yesterday, today, or tomorrow, with a headline "Expected generation +X kWh". This note documents the rough physical model behind that curve and the config it reads. It is the read/visualisation half of the eventual solar load-balancing goal — a forecast to compare against reality, never a control input.

## Source

A keyless [Open-Meteo](https://open-meteo.com/) request — the same host the weather tile already uses — for the hourly **global tilted irradiance** (GTI) variable, at a sub-array's tilt and azimuth, across `past_days=1` … `forecast_days=2` so all three selectable days come back in one call. GTI is returned in **W/m²** as a *preceding-hour mean*, so one hour of it integrates straight to Wh with no sub-hour modelling.

**One request per sub-array (issue #555).** A real roof is rarely one uniform orientation — this home's is ~90% south-facing and ~10% mounted the opposite way. Open-Meteo's `tilt`/`azimuth` params don't support batching multiple orientations into one call (unlike `latitude`/`longitude`, which do), so a multi-orientation array fires one GTI request per sub-array, concurrently over a shared session, and sums the weighted result per hour.

No API key, no account, no cross-repo dependency. See "PVGIS as a source" below for why the dedicated `pvgis` sister repo isn't used as a live source here.

## The estimate

For each hour, per sub-array:

```
expected_W  = kwp · (GTI / 1000) · performance_ratio
expected_Wh = expected_W · 1h            # GTI is an hourly mean
```

`kwp` is that sub-array's peak power, defined at the **1000 W/m² STC reference**, so `GTI / 1000` is the fraction of peak the current irradiance represents. `performance_ratio` (the derate) is shared across all sub-arrays — it folds together every loss the irradiance model does not (inverter efficiency, wiring and thermal losses, soiling, mismatch) into one factor (typically ~0.75–0.85), and isn't orientation-dependent. The day total is the sum of every sub-array's hourly Wh, shown as kWh.

This is a **rough, clearly-labelled estimate**, not a guarantee: by default it ignores panel temperature and horizon shading (see "Panel temperature" and "Horizon / shading profile" below for the optional, off-by-default terms that model each), and it always ignores inverter clipping and snow/soiling events. Treat it as "what a clear-sky-ish day of this weather should roughly yield."

## Config — `config/pv_system.json`

Per-machine, **gitignored** (the repo is public). Copy `config/pv_system.sample.json` and fill in your array — one entry in `arrays` per physically-uniform sub-array (panels sharing a tilt + azimuth):

```jsonc
{
  "arrays": [
    { "kwp": 7.9, "tilt_deg": 15, "azimuth_deg": 0 },    // south-facing majority
    { "kwp": 0.9, "tilt_deg": 15, "azimuth_deg": 180 }   // mounted the opposite way
  ],
  "performance_ratio": 0.8
}
```

| field | meaning | notes |
| --- | --- | --- |
| `arrays` | list of sub-arrays | at least one entry required; a single-orientation roof needs just one |
| `arrays[].kwp` | that sub-array's peak power (kW) | the only required field per entry; must be > 0 |
| `arrays[].tilt_deg` | panel tilt from horizontal | 0–90, clamped; default 30; **always non-negative** |
| `arrays[].azimuth_deg` | panel compass orientation | Open-Meteo convention — **0 = South, −90 = East, 90 = West, 180 = North**; default 0 (due south) |
| `performance_ratio` | shared derate factor | 0–1, clamped; default 0.8; one value for the whole system, not per sub-array. **Its meaning depends on `thermal_model_enabled`** — see "Panel temperature" |
| `thermal_model_enabled` | arm the panel-temperature term | optional boolean, default `false`; only a literal `true` arms it. See "Panel temperature" — turning it on requires migrating `performance_ratio` in the same edit |
| `horizon_profile` | obstruction elevation by azimuth | optional list of `{azimuth_deg, elevation_deg}`, default `[]`; editable from the PV system card regardless of the switch. See "Horizon / shading profile" |
| `horizon_profile_enabled` | arm the horizon/shading term | optional boolean, default `false`; only a literal `true` arms it. See "Horizon / shading profile" |

**Expressing an opposite-mounted panel:** don't use a negative tilt. A panel "tilted −15° facing north" and a panel "tilted +15° facing the 180°/north azimuth" are the physically same orientation under Open-Meteo's tilt≥0 convention — always encode it as `tilt_deg: 15, azimuth_deg: 180` (or whatever `180 − south_azimuth` works out to for a non-south main array).

**Backward compatibility:** the legacy single-orientation flat shape — `kwp`/`tilt_deg`/`azimuth_deg`/`performance_ratio` at the top level, no `arrays` key — still loads, as one implicit sub-array. Existing configs keep working unmigrated; split into `arrays` whenever you're ready to model a second orientation.

A malformed individual sub-array entry is skipped (logged), not a hard failure; if every entry is invalid the config is treated as absent.

Coordinates are **reused from `config/location.json`** (the same file the weather tile reads) — there is no separate lat/lon here. If either `pv_system.json` or `location.json` is absent the forecast simply reports "not configured"; the card shows a one-line note pointing at the editor below and nothing else breaks.

## Editing from the app (issue #561)

The Energy tab's **PV system** card, directly under the forecast card, edits the same file — one summary row per panel row (`kwp` · tilt · compass direction), opened into a staged dialog for peak power / tilt / azimuth, plus the shared performance ratio and the home coordinates inline. Saving is live on the next forecast read: `src/pv_forecast.py` loads the config per request, so there is no cache to clear and no restart.

`config/pv_system.json` remains the source of truth, not a cache of the UI:

- **Hand edits keep working.** The card reads whatever is on disk, including the legacy flat shape (rendered as one row).
- **Keys the app doesn't own survive a save.** A hand-written `_doc` note explaining why a home chose a given kWp or derate is preserved across an edit; only `arrays` / `performance_ratio` (and, when migrating a legacy file, the flat `kwp` / `tilt_deg` / `azimuth_deg` it replaces) are rewritten.
- **Coordinates are not copied here.** The card surfaces lat/lon for convenience but persists them through `PUT /api/location` into `config/location.json` — one house, one place its coordinates live.

Read and write are deliberately **different contracts**. `GET /api/energy/pv-system` inherits the loader's leniency (absent or malformed → "not configured", HTTP 200). `PUT` is strict and returns **400** naming the offending field — `kwp` ≤ 0, tilt outside 0–90, azimuth outside ±180, performance ratio outside 0–1. Silently clamping or dropping a row the user just typed would be a bug, not resilience, so the write path validates on its own rather than reusing the reader's skip-and-clamp parsing.

### PVGIS as a source (evaluated, deferred — issue #555)

Issue #555 asked whether pulling live from [PVGIS](https://joint-research-centre.ec.europa.eu/photovoltaic-geographical-information-system-pvgis_en) (the EU JRC's solar radiation/PV performance API) would make this card's estimate more realistic than Open-Meteo. Checked against PVGIS's own API docs: **`seriescalc`/`PVcalc` return historical or typical-meteorological-year irradiance, not a live day-ahead forecast for a specific real date.** They're built for long-term yield assessment, not "what will tomorrow look like" — the exact use case this card needs for its `tomorrow` selector.

**Decision: defer.** Open-Meteo stays the live source for this card. PVGIS remains what it already was before #555 — a one-time calibration input for hand-tuning `performance_ratio` (see below) — not a live dependency, and not built out further as a calibration *feature* in this issue. Revisit only if a concrete need for a periodic "does our tuning still match PVGIS's typical-year model" cross-check shows up.

### Choosing `performance_ratio` (and reading it off a PVGIS system)

`performance_ratio` is the single knob that turns clear-sky irradiance into a believable yield, so it is worth setting deliberately rather than leaving at the 0.8 default. Crucially, **it is not the same number as a PVGIS "system loss"**, even though it is tempting to set `1 − loss`:

- **PVGIS** applies its `loss` percentage (cabling, inverter, soiling, mismatch) *on top of* a separate, physics-based **panel-temperature** correction — hot panels lose efficiency, and PVGIS models that hour by hour.
- **This model has no temperature term by default.** It scales GTI straight to power, so `performance_ratio` has to absorb *both* the PVGIS-style system losses *and* the temperature loss PVGIS would have handled separately. (With `thermal_model_enabled: true` that stops being so — the temperature loss is modelled and the ratio becomes system-loss-only. Read "Panel temperature" below **before** changing either value.)

So translating a PVGIS setup with the thermal term off, don't copy `1 − loss/100` verbatim — subtract a further few points for thermal loss. Worked example, using the values in the sister `pvgis` repo's `.env` (this home's own array has since been expanded to **8.8 kWp** — see `config/pv_system.json`; the translation method below is what matters here, not the peak-power figure):

| PVGIS input | value | → this model |
| --- | --- | --- |
| `HOME_PEAKPOWER_KW` | 8.0 | `kwp: 8.0` |
| `HOME_TILT_DEG` | 35 | `tilt_deg: 35` |
| `HOME_AZIMUTH_DEG` | 0 | `azimuth_deg: 0` (same 0 = S convention) |
| `HOME_LOSS_PCT` | 14 | system loss only → 0.86 |
| *typical thermal loss* | ~6% | the part PVGIS models separately |
| **combined** | | **`performance_ratio: 0.80`** |

Empirically this matters: 0.86 (the raw `1 − loss`) forecast ≈ 51 kWh for a clear June day, while PVGIS's own annual model lands ~45–48 kWh — 0.80 brings the estimate back in line. If you have measured generation, tune `performance_ratio` so the dashed forecast sits over the filled actual curve on clear days.

### Panel temperature (issue #591) — optional, off by default

The constant thermal allowance folded into `performance_ratio` above is the weakest part of that translation: the real loss ranges from ~0% at a 25 °C cell to ~13% at 62 °C, and one number cannot track it. `thermal_model_enabled` replaces the constant with a physical term.

**The model.** `temperature_2m` rides along in the *same* Open-Meteo request as GTI (no extra call), cell temperature comes from the standard NOCT form, and power is scaled by the panel's power temperature coefficient:

```
T_cell = T_air + (NOCT − 20) / 800 · GTI          # NOCT = 45 °C
factor = 1 + γ · (T_cell − 25)                     # γ = −0.0035 / °C, floored at 0
expected_Wh = kwp · (GTI / 1000) · performance_ratio · factor
```

γ and NOCT are constants in `src/pv_forecast.py`, not config fields: issue #578 fitted them against this array's own measured output and they land within ±5% for every morning and midday hour, and there is no UI through which a user could recalibrate them meaningfully. **Wind speed is deliberately not modelled** — the still-air NOCT form already fits within measurement noise here, and the residual gap it does *not* close is the afternoon one, which is geometric (horizon shading, #578 part b), not thermal. A wind term would only appear to absorb it.

**Turning it on is a migration, not a toggle.** The two settings are read together:

| `thermal_model_enabled` | what `performance_ratio` means | this array |
| --- | --- | --- |
| `false` (default) | combined derate — system losses **plus** a constant thermal allowance | 0.80 |
| `true` | system losses **only**; the thermal loss is now modelled hour by hour | ~0.88 (`1 − 0.14` PVGIS loss) |

Flipping the switch while leaving `performance_ratio` at 0.80 would subtract the thermal loss twice and under-forecast by roughly 10%. That combination is **refused, not computed**: `src/pv_system_config.py`'s `thermal_migration_error()` is the single place that names it, and both halves of the app route through it — the strict writer raises (so the Energy-tab editor gets a **400** explaining the conflict) and the forecast returns `{ "available": false, "reason": "thermal_ratio_unmigrated" }` rather than a plausible-looking wrong curve. The floor is `MIN_THERMAL_PERFORMANCE_RATIO` (0.85); migrate both values in the same edit.

**With the switch off nothing changes at all** — not the numbers, not the payload's keys, not even the upstream request (`temperature_2m` is only asked for when the term is armed). That is the deliberate default: the committed config leaves it off, and only a literal JSON `true` arms it, so a stray `"yes"` in a hand-edited file cannot change what the card predicts by truthiness.

### Horizon / shading profile (issue #578 part b) — optional, off by default

The residual gap the thermal term above does *not* close is the afternoon one — a knee that repeats to the minute across clear days, the signature of a fixed obstruction (a ridge, a neighbouring roof, a tree line), not weather or panel temperature. `horizon_profile_enabled` arms a hand-entered obstruction-elevation-by-azimuth profile that attenuates the direct-beam share of an hour once the sun drops behind it. **Currently off pending an installer conversation about the real geometry** — the profile is buildable now (editable from the PV system card in the panel-row's staged-dialog style), but nothing it contains is applied until the switch is flipped by hand.

**The profile.** A list of `{azimuth_deg, elevation_deg}` points — the same shape PVGIS/PVsyst use for a horizon line. `azimuth_deg` is **compass, clockwise from true north** (0=N, 90=E, 180=S, 270=W) — `src/sun_position.py`'s convention, deliberately *not* `arrays[].azimuth_deg`'s Open-Meteo south-relative one, because a horizon point is compared against a *computed sun position*, never a panel orientation. `elevation_deg` (0–90) is how high the obstruction stands above the horizontal at that azimuth. Two or more points interpolate linearly between their azimuth-sorted neighbours, wrapping at the 360°/0° boundary; a single point applies to every azimuth; an empty list is a no-op (see below).

**The model.** For each hour, per sub-array, the sun's azimuth/elevation is computed from `src/sun_position.py` (reused from the #590 diagnostic — no second solar-position implementation) at the hour's Open-Meteo timestamp. If the sun's elevation is at or below the profile's interpolated obstruction elevation for that azimuth, the hour's GTI is scaled down to its **diffuse-only share**:

```
if sun_elevation_deg <= horizon_elevation_deg(profile, sun_azimuth_deg):
    expected_Wh *= diffuse_radiation / (direct_radiation + diffuse_radiation)
```

Open-Meteo does not publish a direct/diffuse split of the *tilted* GTI figure itself, so this uses the **horizontal** `direct_radiation` / `diffuse_radiation` split (fetched in the same request as GTI, only when the term is armed) as a stand-in for "what a shaded panel still receives" — an obstruction blocks the sun's disc, not the sky. This is deliberately approximate, the same spirit as the thermal term's constants: a from-scratch beam/diffuse transposition onto the tilted plane would be a second, independently-error-prone model stacked on top of Open-Meteo's own.

**Switch-on + empty/unconfigured profile is a no-op**, not a zeroed evening — an empty profile reports "no obstruction anywhere" (−90° everywhere), so the sun is always "above" it and the shading branch never fires. Building the editor and populating it is safe at any time; only a hand-set `horizon_profile_enabled: true` changes what the forecast predicts.

**With the switch off nothing changes at all** — same contract as the thermal term: no `direct_radiation`/`diffuse_radiation` in the request, no change to the numbers. The committed config leaves it off, and only a literal JSON `true` arms it.

## Caching, rate limits and backoff (issue #597)

The card is polled by an always-on dashboard, and Open-Meteo's hourly irradiance only changes on an hourly grid, so `src/pv_forecast.py` caches the response per `(location, array-set, day)` for 15 minutes and reuses one `aiohttp.ClientSession` across calls — the same pattern `src/huawei_client.py` uses for FusionSolar. A render inside the cache window costs no upstream request at all; the sun-position diagnostic (#590) shares the same cache entry for a date it also fetches.

A `429` from Open-Meteo gets its own reason, `rate_limited`, distinct from `unreachable` — the server answered, it just refused this request rate. It also opens a backoff window (60 s, doubling to a 900 s ceiling per consecutive 429) during which no further upstream call is attempted at all, and — as long as one is still less than 6 hours old — the last successfully-fetched curve is served instead of a blank card, whether the 429 opened the backoff just now or an earlier one is still active.

## Endpoint

`GET /api/energy/forecast?day=yesterday|today|tomorrow` →

```jsonc
{
  "available": true,
  "day": "today",
  "expected": [{ "hour": 0, "wh": 0.0 }, /* … 24 hourly points … */],
  "expected_total_kwh": 18.4,
  "actual": [{ "hour": 0, "wh": null }, /* … or null for tomorrow … */],
  "system": {
    "arrays": [
      { "kwp": 7.9, "tilt_deg": 15, "azimuth_deg": 0 },
      { "kwp": 0.9, "tilt_deg": 15, "azimuth_deg": 180 }
    ],
    "total_kwp": 8.8,
    "performance_ratio": 0.8
  }
}
```

`actual` is the measured generation for that day from the local energy-history DB (`hourly_day`), 24 hourly points where a `null` hour is an asleep inverter or an hour with no sample (drawn as a gap, never a 0) — the same "asleep is not zero" rule the live chart uses. `tomorrow` has no actuals, so `actual` is `null`.

`system` echoes back the sub-array params the curve was computed from, so the card can show them in a caption (e.g. `7.9 kWp · 15° · S  +  0.9 kWp · 15° · N · PR 0.80`) and you can sanity-check the inputs at a glance. Present only on an available forecast. It carries an extra `thermal_model: { gamma_per_c, noct_c }` block **only** when the panel-temperature term is armed — with the term off the payload's keys are exactly what they were before #591.

Always HTTP 200: when the array/location is unconfigured or Open-Meteo is unreachable it returns `{ "available": false, "reason": … }` and the card keeps its note — the forecast is decorative, never a 500. The reasons are `not_configured`, `no_location`, `too_old`, `unreachable`, `rate_limited` (Open-Meteo answered with a 429 — see "Caching, rate limits and backoff" above), `no_data`, and `thermal_ratio_unmigrated` (the panel-temperature term armed on top of an un-migrated `performance_ratio` — see "Panel temperature" above).

## Reading the model back: the sun-position diagnostic (issue #590)

The model above is a *forecast*. The Energy tab's folded-away **Sun-position
diagnostic** is its mirror: it takes what was already measured and what this
same model already predicts, and plots the ratio against where the sun actually
was. Nothing in the model changes — that is the point of keeping it a separate
endpoint (`GET /api/energy/sun-overlay?date=YYYY-MM-DD`) rather than another
field on the forecast payload.

**Effective performance ratio.** The quantity plotted is `actual_Wh / (kWp ·
GTI)` — what the array really delivered per unit of plane-of-array irradiance.
Since `expected_Wh = kWp · GTI · PR`, that denominator is already in hand:

```
effective_PR = PR · actual_Wh / expected_Wh
```

No second irradiance source, no second model. For a multi-orientation roof the
denominator is the kWp-weighted mean GTI across sub-arrays — the same sum the
forecast takes.

**Why azimuth is the x-axis.** `performance_ratio` folds inverter, wiring,
thermal and soiling losses into one constant, and a constant cannot describe
shading: an obstruction bites at a *sun position*, not at a time. Plotted
against azimuth, a fixed obstruction is a knee that lands in the same place
every day, while weather is scatter that does not. Sun position comes from
`src/sun_position.py` (NOAA's low-precision algorithm, stdlib-only), evaluated
at the mid-point of each hour.

**Short-coverage hours are excluded, and named.** An hour whose PV integral
rests on only part of the hour is under-measured, not low — plotted raw it is a
depressed performance ratio at whatever azimuth the sun happened to be at, i.e.
a fabricated shading signature. The overlay drops any hour below
`MIN_TRUSTED_COVERAGE` (0.75) and reports the count under the chart, so an
absent hour reads as an absent hour. Hours with no meaningful modelled
irradiance (night, deep twilight) are simply not plotted: nothing is wrong with
them, and dividing by a near-zero denominator would swamp the curve.

**Reach.** Hourly rollups are retained 400 days, but Open-Meteo's `past_days`
tops out around 92; a date beyond that returns `{ "available": false, "reason":
"too_old" }`. A date within reach that predates the app's own history is an
*empty* overlay, not an error.
