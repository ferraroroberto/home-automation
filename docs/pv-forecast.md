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

This is a **rough, clearly-labelled estimate**, not a guarantee: it ignores panel temperature, horizon shading, inverter clipping, and snow/soiling events. Treat it as "what a clear-sky-ish day of this weather should roughly yield."

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
| `performance_ratio` | shared derate factor | 0–1, clamped; default 0.8; one value for the whole system, not per sub-array |

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
- **This model has no temperature term.** It scales GTI straight to power, so `performance_ratio` has to absorb *both* the PVGIS-style system losses *and* the temperature loss PVGIS would have handled separately.

So translating a PVGIS setup, don't copy `1 − loss/100` verbatim — subtract a further few points for thermal loss. Worked example, using the values in the sister `pvgis` repo's `.env` (this home's own array has since been expanded to **8.8 kWp** — see `config/pv_system.json`; the translation method below is what matters here, not the peak-power figure):

| PVGIS input | value | → this model |
| --- | --- | --- |
| `HOME_PEAKPOWER_KW` | 8.0 | `kwp: 8.0` |
| `HOME_TILT_DEG` | 35 | `tilt_deg: 35` |
| `HOME_AZIMUTH_DEG` | 0 | `azimuth_deg: 0` (same 0 = S convention) |
| `HOME_LOSS_PCT` | 14 | system loss only → 0.86 |
| *typical thermal loss* | ~6% | the part PVGIS models separately |
| **combined** | | **`performance_ratio: 0.80`** |

Empirically this matters: 0.86 (the raw `1 − loss`) forecast ≈ 51 kWh for a clear June day, while PVGIS's own annual model lands ~45–48 kWh — 0.80 brings the estimate back in line. If you have measured generation, tune `performance_ratio` so the dashed forecast sits over the filled actual curve on clear days.

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

`system` echoes back the sub-array params the curve was computed from, so the card can show them in a caption (e.g. `7.9 kWp · 15° · S  +  0.9 kWp · 15° · N · PR 0.80`) and you can sanity-check the inputs at a glance. Present only on an available forecast.

Always HTTP 200: when the array/location is unconfigured or Open-Meteo is unreachable it returns `{ "available": false, "reason": … }` and the card keeps its note — the forecast is decorative, never a 500.

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
