# Energy monitoring, history and tariff

## Energy monitoring & history

The PWA splits into five tabs, each opening with the same page header —
the tab's title, the theme toggle, and a **Settings** gear: **Home** (a pure
dashboard — the weather as the header's live line, then a **House**
card with the arm control, the compact ☀️ Solar · 🏠 Home · 🗼 Grid flow, who is
home and an exception chip only when something needs you, a **Climate** group
with the AC rows and their power switches, and **Next up**: wake alarms, timers
and reminders as time-led rows),
**AC** (one row per unit with its power switch; the unit sheet holds the rest),
**Energy** (a glance card with the live
☀️ Solar · 🏠 Home · 🗼 Grid flow row — the arrows and the Grid node's name,
Exporting / Importing, say which way power flows — and one line of
self-sufficiency and self-consumption now; a **Today** card with two meters,
Made (share used in the house) and Used (share covered by the sun), plus € saved
on self-consumed PV at the configured tiered rate, CO₂ avoided with its
tree-years, and export income; one **History** card with one period picker,
Live · Day · Week · Month · Year · All, and an **Energy / Money** switch — Live is
the all-positive Generation/Grid-supplied/Consumption chart of the last hour,
Energy the history chart with its five totals, Money the tariff estimate with a
row per time-of-use period and every figure in an **All figures** sheet, see
*Electricity tariff* below; and the **Solar forecast**), **🔌 Devices** (every local device on one surface — the Power glance card
with the UPS, then Lights, Blinds, Plugs, Circuits, Home Assistant and Network
as open groups — see below), and **🛡️ Security** (RISCO alarm controls, schedules, event
log, cameras, detectors, and presence).

**Live header line and tab badge.** Beside each tab's title, AC, Energy,
Devices and Security show one live line: what needs you, in its tone (AC "1
offline", Security "Triggered" in red or "2 trouble" in amber, Devices "On
battery" or "UPS battery low", Energy "Live unavailable"), else a plain fact
("2 running", "Exporting 356 W", "3 on · 235 W", "24 detectors"). It only
reports a live read, never a restored snapshot. The **Security** tab carries a
count badge seen from every tab: amber with the detector-trouble count, red
with "1" while the alarm is triggered; it updates whenever the alarm is polled
(on Home and Security). Home AC rows are the shared row: tap the row to open
the unit's sheet, tap the switch for power. Every detail sheet closes the same
way (×, Esc, tapping outside) and hands focus back to what opened it; an editor
saves only on **Save**, a device sheet such as Wi-Fi saves as you change it and
closes on **Done**.

**Settings** is not a tab: the gear in any page header opens it. It
is inset groups, each row showing its current value and opening a sheet:
**Display** (text size, inline), **Notifications** (Alarm and UPS power, the
Telegram toggles, each row reading "n of N on"), **Home & people** (Home
location, Places, Presence automation), **Energy** (PV system, Export
compensation, Solar boost, Sun-position check), **Voice** (Search engine, What
can I say?) and **Diagnostics** (Activity log, and the Record navigation log
switch, inline). Each sheet saves as you change it and closes on **Done**; a
saved item (a place, a panel row) keeps its staged editor on top. Network
lives in the Devices tab's Network group; a PWA last left on the retired Net tab
reopens once on Settings.

**Text size** is the first group in Settings: Small / Default / Large, kept
per device in `localStorage` under `home-automation.textsize` and applied to the
root font-size (93.75% / 100% / 112.5%) before first paint by the same inline
`<head>` script that stamps the theme. The viewport is zoom-locked, so this is
the app's own way to enlarge text (WCAG 1.4.4). Type is rem-based and scales;
geometry (nav, rows, hit targets, control height, icons) stays in px. The
control is the vendored `_vendored/text-size/` component from
`project-scaffolding`, byte-identical; never edit it per-app.

On desktop the tabs are a top segmented control; on a phone / installed PWA they
become a floating bottom tab bar with stroke icons (mirroring the `app-launcher`
nav). Collapsible sections (Settings, and Devices)
share one icon-led summary style; Security's lists are open groups.

While the webapp runs, a background **sampler** (started in the FastAPI
lifespan, so it lives and dies with the tray's uvicorn process) persists the
live energy flow to a server-side SQLite database. Recent raw samples feed the
live chart; completed hours are folded into compact rollups that daily and
monthly views group from. An **asleep inverter is stored as no PV reading**, not
a misleading 0, so the charts show a gap and aggregates flag `pv_missing`.

**Missing is not low.** An hour the cloud feed only partly covered is
*under-measured*, not dim: its Wh is an integral over the minutes that arrived,
so a feed outage on a cloudless morning would otherwise plot as a production
collapse. Rollups therefore also record how much of each hour actually carried
data, and hourly buckets expose `pv_coverage` (0–1) plus a `pv_gap` flag when
that falls below 75%. The Solar forecast card draws those hours as hollow,
dashed **projections** rather than measurements, and both it and Today's
generation say how long the feed was offline — so a dead upstream feed reads as
exactly that, never as a dead inverter. Stored totals are never inflated: only
the forecast overlay is projected, and the day's kWh figures stay as measured.

- **Storage:** `webapp/energy_history.sqlite3` (gitignored, per-machine runtime
  data — never committed).
- **Endpoints:** `GET /api/energy` (live snapshot), `GET /api/energy/today`
  (today's totals for the split + savings cards, plus `gap_hours` — how long the
  PV feed was offline mid-hour today), `GET /api/energy/history?minutes=N`
  (raw samples for the live chart), `GET /api/energy/aggregate?range=day|week|month|year|total`
  (energy-per-bucket, Wh — `day` is a 24h fill-up frame; `week`/`month`/`year` are
  rolling data-only windows; `total` is all retained history), and
  `GET /api/energy/cost?range=day|week|month|year|total` (tiered cost & savings
  breakdown over the same windows — per-period consumption/grid/solar/cost/savings
  plus a fixed-cost + estimated-bill summary; see *Electricity tariff* below).
- **Cadence/retention knobs** (`.env`, all optional):

| Key | Default | Meaning |
|-----|---------|---------|
| `ENERGY_SAMPLER_ENABLED` | `true` | Master switch. `false`/`0` serves live + existing history but persists nothing (used by the e2e suite and dev runs so they don't poll FusionSolar). |
| `ENERGY_PERSIST_INTERVAL_S` | `60` | Seconds between persisted samples. For a 5-minute cloud source (FusionSolar) the data simply won't change faster than the source. |
| `ENERGY_COMPACT_INTERVAL_S` | `3600` | How often completed hours are folded into rollups and old raw data pruned. |
| `ENERGY_RAW_RETENTION_DAYS` | `7` | How long raw per-sample rows are kept (feeds the live chart). |
| `ENERGY_HOURLY_RETENTION_DAYS` | `400` | How long hourly rollups are kept (daily/monthly views group from these). |

The live display refreshes every ~5 s while the Energy tab is open and every
30 s otherwise; that cadence is a frontend constant (it never persists faster
than `ENERGY_PERSIST_INTERVAL_S`). Energy (Wh) is integrated from the samples
(rectangular rule, gaps capped) — a household-monitoring estimate, not a
billing-grade meter read.

### Electricity tariff (cost & savings)

The Energy tab's **History › Money** view (the cost & savings breakdown) prices
grid energy per time-of-use period and values the self-consumed PV at the same avoided rate. Rates come from
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
  solar" cost. Export is credited per hour using the latest dated `export_rates`
  entry effective at that time, surfaced with grid cost and avoided cost in the
  **Money** view, and already netted into the estimated bill. Dated rates can
  be added, edited, or deleted in **Settings › Export compensation** (Money's
  **Export rate** row opens it) without
  restarting; an optional
  24-value hourly schedule overrides the dated default, and legacy
  `export_eur_kwh` files still load. The matching **Energy** view shows
  production, consumption, solar consumed, grid imported, and solar exported.

How the period prices and the model are derived from a real PVPC 2.0TD invoice —
including the PVPC hourly-market approximation and the bono-social handling — is
documented in [`tariff-model.md`](tariff-model.md).

> **Data-retention caveat.** The breakdown is computed from the local history DB,
> so the **Year** and **All** windows only fill in as the sampler accrues
> data (hourly rollups are kept `ENERGY_HOURLY_RETENTION_DAYS`, default ~400
> days). A freshly-started instance shows mostly empty long windows until history
> builds up — this is an estimate from monitored data, not your utility's meter.
> While history is younger than a window's nominal length (e.g. a few weeks old,
> vs. `month`'s 30 days or `year`'s 365), that window's fixed standing charge is
> prorated over the *actual* retained span rather than the full nominal one —
> otherwise `year` would charge a full year of fixed cost against a few weeks of
> real consumption. Expect `month`/`year`/`total` to show identical consumption,
> cost, and fixed-charge figures until history genuinely exceeds 30/365 days.
