# Activity log and usage log

## Activity log (events & telemetry)

**Settings › Diagnostics → Activity log** opens an overlay (not a tab — an admin/telemetry surface) that shows the unified, filterable history of everything the home does: alarm arm/disarm, plug toggles, UPS power lost/restored, presence transitions, and RISCO panel events. It is the read surface over `src/telemetry.py`'s `events` store, which makes the `logs/*.jsonl` trail and the RISCO event feed queryable.

Events are recorded by their producers automatically: the shared `src/activity_log.append_activity` writer mirrors every alarm/power/presence event into the store, plug toggles are recorded in the Tuya router, and `GET /api/security/events` persists the live RISCO feed (deduped, so re-polling and restarts never double-insert). The overlay reads `GET /api/activity?domain=&type=&since=&limit=`, filtering **server-side** via the domain dropdown and the type box (a **substring** match — typing `w` finds `power_w`). Rows are labelled with the device's rename (e.g. "<device rename>") rather than a raw id, and a **"What do these mean?"** legend inside the overlay explains every event type, reading metric, and the severity colours. Readings with no value at all (an offline or non-metering device) are **not** stored — the device simply shows a gap, so the log isn't flooded with empty `—` rows.

The overlay also has a **Readings** view (toggle at the top): periodic device telemetry — HVAC room/set temperatures, plug watts/volts/amps/kWh, UPS load/charge/runtime, Elgato light state — captured by a background **telemetry sampler** (`app/webapp/telemetry_sampler.py`), a sibling of the energy sampler owned by the same webapp lifecycle. It runs on a gentle cadence (default 5 min — temperature/load trends don't need 60 s resolution) and isolates failures per domain, so one offline plug never stops the rest. Energy stays in its own `energy_history` store; presence is event-driven, so its reading gate defaults off.

The store lives at `webapp/telemetry.sqlite3` (gitignored, per-machine runtime, WAL mode, modeled on `energy_history.py`). Config + retention knobs (`.env`, all optional):

| Variable | Default | Meaning |
| --- | --- | --- |
| `TELEMETRY_SAMPLER_ENABLED` | `true` | Master switch for the reading sampler. `false`/`0` serves events + existing readings but captures no new readings (used by the e2e suite and dev runs). |
| `TELEMETRY_SAMPLE_INTERVAL_S` | `300` | Seconds between reading snapshots. |
| `TELEMETRY_SAMPLE_HVAC` / `_PLUGS` / `_UPS` / `_LIGHTS` / `_CIRCUITS` | `true` | Per-domain gates — turn a flaky/slow domain off without disabling the rest. `_CIRCUITS` samples per-channel power from the Athom CT clamps, keyed `"<meter_id>:<channel>"`. Presence has no reading collector — it is captured as events only. |
| `TELEMETRY_READINGS_RETENTION_DAYS` | `7` | How long **raw** device readings are kept. Short by design — the history behind them lives in the hourly rollups below. |
| `TELEMETRY_EVENTS_RETENTION_DAYS` | `400` | How long discrete events are kept — far longer than readings, since events are rare and human-meaningful. |
| `TELEMETRY_ROLLUP_RETENTION_DAYS` | `400` | How long hourly reading rollups are kept, matching the events window. |

**Hourly rollups.** Raw readings are kept for a week, but they are not the limit of what the house remembers: before each prune, every *completed* hour is folded into a `rollup_hourly` row per `(hour, domain, entity, metric)` carrying the hour's `avg`/`min`/`max` and kept ~400 days. Raw stays the live view; the rollups are the history. Three things the aggregation is deliberate about:

- **Asleep is still not zero.** An hour whose series never produced a number stores `NULL` averages, never `0`.
- **Coverage, not just counts.** Each row records `covered_s` — how much of the hour its aggregate actually rests on — so a dead sampler is distinguishable from an idle device. Below 75% the hour is an *under-measurement*, not a low reading. Measured over a real 168-hour fold of the live store, 93% of series-hours land at ≥0.9 coverage, and the flagged ones are genuinely short (mostly Elgato lamps, which leave the LAN when switched off at the wall).
- **Categorical metrics are decided, not averaged.** `operation_mode`, `fan_speed`, `switch_on` and `status` have no mean, so a rollup records the hour's **last** value plus how many times it changed (`txt_changes`) — one integer that keeps a settled hour distinguishable from a thrashing one.

Cost, measured rather than estimated: ~78 bytes/row, ~108 KB/day, ~43 MB at the 400-day steady state — against 18.3 MB for a single week of raw. The store is write-side only for now; nothing reads the rollups yet.

> **Extensible by design:** the `readings`/`events` tables are narrow (one row per observation/event) with a JSON sidecar column, so a new device type or field becomes new *rows*, never an `ALTER TABLE`. A new domain just needs a small pure adapter in `src/telemetry_adapters.py` (mapping its reading object → rows) wired into the sampler.

## Usage log (views and taps)

So a design review can read how the app is really used instead of asking, the PWA records which tabs are opened and which main controls are tapped, and on which device class. It is the vendored `usage-log` component from `project-scaffolding`, both halves byte-identical: `app/webapp/static/_vendored/usage-log/usage-log.js` batches events and posts them to this app's own `POST /api/usage/events`, and `src/usage_log.py` stores them in `usage.sqlite3` under the fleet runtime-data dir for `home-automation` (`src/runtime_data.py`). `GET /api/usage/summary` and project-scaffolding's `scripts/usage_summary.py` read it back.

- **What is stored:** event kind, view id (the tab), control id, a random per-visit session id, device class (phone/tablet/desktop), OS class and a server timestamp. Never names, values, URLs with queries, IPs or the user-agent; the server drops anything outside that.
- **Control ids** are developer-chosen constants in `data-track` (`ac.power`, `energy.history-range`, `settings.text-size`, …), never derived from what an element says. Add one to a control a review would ask about, not to every button.
- **Auth:** the router sits behind the same bearer middleware as every `/api` route; the client sends the token per request, like `api()`.
- **Tests:** `USAGE_LOG_DB_PATH` overrides the file. The backend suite and every e2e boot point it at a temp file, so no test writes to the real log.
