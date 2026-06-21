# Solar generation forecast — the model

The Energy tab's **Solar forecast** card (issue #39) overlays an *expected
generation* curve on the day's measured generation, for yesterday, today, or
tomorrow, with a headline "Expected generation +X kWh". This note documents the
rough physical model behind that curve and the config it reads. It is the
read/visualisation half of the eventual solar load-balancing goal — a forecast to
compare against reality, never a control input.

## Source

A single keyless [Open-Meteo](https://open-meteo.com/) request — the same host the
weather tile already uses — for the hourly **global tilted irradiance** (GTI)
variable, at the array's tilt and azimuth, across `past_days=1` … `forecast_days=2`
so all three selectable days come back in one call. GTI is returned in **W/m²** as
a *preceding-hour mean*, so one hour of it integrates straight to Wh with no
sub-hour modelling.

No API key, no account, no cross-repo dependency. The dedicated `pvgis` sister
repo is the more faithful PV-estimate path; this card deliberately stays
self-contained and approximate (the source chosen for issue #39).

## The estimate

For each hour:

```
expected_W  = kwp · (GTI / 1000) · performance_ratio
expected_Wh = expected_W · 1h            # GTI is an hourly mean
```

`kwp` is the array's peak power, defined at the **1000 W/m² STC reference**, so
`GTI / 1000` is the fraction of peak the current irradiance represents.
`performance_ratio` (the derate) folds together every loss the irradiance model
does not — inverter efficiency, wiring and thermal losses, soiling, mismatch —
into one factor (typically ~0.75–0.85). The day total is the sum of the hourly
Wh, shown as kWh.

This is a **rough, clearly-labelled estimate**, not a guarantee: it ignores
panel temperature, horizon shading, inverter clipping, and snow/soiling events.
Treat it as "what a clear-sky-ish day of this weather should roughly yield."

## Config — `config/pv_system.json`

Per-machine, **gitignored** (the repo is public). Copy
`config/pv_system.sample.json` and fill in your array:

| field | meaning | notes |
| --- | --- | --- |
| `kwp` | installed peak power (kW) | the only required field; must be > 0 |
| `tilt_deg` | panel tilt from horizontal | 0–90, clamped; default 30 |
| `azimuth_deg` | panel compass orientation | Open-Meteo convention — **0 = South, −90 = East, 90 = West, 180 = North**; default 0 (due south) |
| `performance_ratio` | derate factor | 0–1, clamped; default 0.8 |

Coordinates are **reused from `config/location.json`** (the same file the weather
tile reads) — there is no separate lat/lon here. If either `pv_system.json` or
`location.json` is absent the forecast simply reports "not configured"; the card
shows a one-line note pointing at the sample and nothing else breaks.

## Endpoint

`GET /api/energy/forecast?day=yesterday|today|tomorrow` →

```jsonc
{
  "available": true,
  "day": "today",
  "expected": [{ "hour": 0, "wh": 0.0 }, /* … 24 hourly points … */],
  "expected_total_kwh": 18.4,
  "actual": [{ "hour": 0, "wh": null }, /* … or null for tomorrow … */]
}
```

`actual` is the measured generation for that day from the local energy-history DB
(`hourly_day`), 24 hourly points where a `null` hour is an asleep inverter or an
hour with no sample (drawn as a gap, never a 0) — the same "asleep is not zero"
rule the live chart uses. `tomorrow` has no actuals, so `actual` is `null`.

Always HTTP 200: when the array/location is unconfigured or Open-Meteo is
unreachable it returns `{ "available": false, "reason": … }` and the card keeps
its note — the forecast is decorative, never a 500.
