# Huawei solar / energy

## Huawei solar / energy

The dashboard shows the home's live energy flow (☀️ Solar · 🏠 House · ⚡ Grid ·
♻️ Net) as the read-side foundation of the eventual solar load-balancing
automation (shift HVAC load to match PV).

**Hardware:** a **Huawei SUN2000-8K-LC0** hybrid inverter (8 kW, single-phase)
with an 8.8 kWp array and an RS485 power sensor on the grid connection. No
battery is fitted.

**Source: local Modbus TCP first, the FusionSolar cloud as fallback.** A **Smart
Dongle `SDongleA-05`** is fitted to the inverter and wired to the LAN, so `src/huawei_modbus.py` reads PV production and the grid exchange
straight off the inverter's holding registers — about **1 s old**, against the
portal's 5-minute grid and the further few minutes it runs behind. On any
failure `src/huawei_client.py` falls back to the cloud path, and the
serving source is logged whenever it changes so a fallback that quietly becomes
permanent is visible. A cloud point older than `FUSIONSOLAR_MAX_STALENESS_S` is
still treated as **stale** and reported unavailable rather than served as live,
so a frozen upload cannot flat-line the chart.

Enable Modbus on the dongle once, in the owner portal: **FusionSolar Web →
Monitoring → your dongle → Configuration → ModBus-TCP → `Enable
(unrestricted)`**. Then set `HUAWEI_MODBUS_MAC` (and optionally
`HUAWEI_MODBUS_HOST` as a cold-start hint) in `.env` — see `.env.example`. With
neither set the local path is simply disabled and the cloud serves, which is
also what keeps CI off the real inverter.

> **Sign convention — register `37113` is positive when *exporting*,** the exact
> opposite of the cloud's `meterActivePower` (positive when *importing*). Each
> source splits its own signed figure into `grid_import_w` / `grid_export_w` in
> exactly one place, and the two must never be collapsed into a shared helper.
> Proven on the live system rather than inferred: the cloud reported exporting
> 1004 W while `37113` read +1015 W two minutes later, and across a 220-second
> window with `37113` positive throughout, the meter's cumulative register
> `37119` advanced at the matching rate while `37121` stayed frozen.

> **The cloud is not belt-and-braces — keep it.** The dongle self-reboots when it
> loses its route to the gateway or to the FusionSolar cloud, so both paths tend
> to fail *together*; dropping the cloud would lengthen outages, not shorten
> them. The cloud also remains the **only** source for history backfill (the
> registers are instantaneous, with no series) and for today's cumulative kWh
> counters (the meter's own counters are lifetime totals, which would corrupt
> the Home Assistant `TOTAL_INCREASING` "energy today" sensors).

> **Only one client may talk to the dongle at a time** — Huawei's own guide, and
> observed: opening a second session dropped the first mid-read. This app must
> be the sole collector, so let Home Assistant read `/api/energy` and never poll
> the dongle itself. Reads are serialised behind a lock, one snapshot is reused
> for `HUAWEI_MODBUS_CACHE_TTL_S` (default 5 s) to hold the on-device cadence
> down, and the session is closed after every cycle. The dongle also **moves**:
> it holds a DHCP lease, so the MAC is the authoritative address and a failed
> read re-resolves it via `src.network_client.resolve_ip_by_mac` before conceding
> to the cloud.

**Reading around the source's gaps.** The portal serves the day as parallel
5-minute series, and the newest data is not always trustworthy. Two hazards,
both measured live and both handled in `src/huawei_client.py`:

- The `fusion_solar_py` helper returns the newest non-null sample of *each
  series independently*, which mixes buckets whenever one lags. We read one
  aligned bucket instead.
- A bucket can be half-written without being marked `--`: consumption is a
  *derived* series here (`existUsePower: false`) and sits at a placeholder
  `0.000`. Taken at face value it would render the house at 0 W and the grid
  exporting more than the array generated.

The completeness test is the flow identity itself — `productPower +
meterActivePower == usePower`, which held to **exactly zero** in 32 of 39
buckets across a live day, every exception being a placeholder. A bucket that
does not balance is not yet real, so the read steps back to the last one that
does. Those holes are often *permanent* (two runs were still unusable an hour
later), which is why the staleness window is 30 minutes rather than 15.

**Failures back off; they are not retried at poll rate.** A failed read leaves nothing to put in the response cache, so without a guard every request re-attempts the portal login — and the PWA polls energy every 5 s. Unguarded, an expired session becomes a login storm (the tray re-attempting every ~17 s for minutes), after which the portal refuses it outright while a one-shot CLI login still works. After a failure the client stays quiet for 60 s, doubling to a 15-minute ceiling, and keeps serving the last good payload meanwhile — the staleness guard above still decides when that stops counting as live, so backing off never passes stale data off as fresh.

**Sign convention worth knowing.** FusionSolar reports a single signed
`meterActivePower` whose sign is the *opposite* of the one the portal's own
device page shows. The identity `productPower + meterActivePower = usePower`
pins it down: **positive means importing**. `src/huawei_client.py` splits it into
`grid_import_w` / `grid_export_w` so that logic lives in exactly one place.

**When live data is unavailable, the app says why.** If the portal
read fails or returns only stale data, the live-flow tile shows an inline note
instead of bare `—`, and it distinguishes the two causes: solar still reading
means the inverter is fine and only the power sensor is bad (a hardware fault
worth chasing), whereas nothing reading at all is simply no data from the
source. The cumulative/history cards keep
rendering from their own sources. More broadly, a hard data-fetch failure on
the Energy, Plugs, or Security tab raises a single error **toast** naming the
source and reason — surfaced once per outage (the tabs poll every few seconds, so
it does not repeat while a source stays down) and re-armed on recovery.

Config in `.env`:

| Key | Meaning |
|-----|---------|
| `FUSIONSOLAR_USER` / `FUSIONSOLAR_PASSWORD` | FusionSolar portal account. Prefer a **plant-owner** account over the installer's. |
| `FUSIONSOLAR_SUBDOMAIN` | Regional host prefix of your portal URL, e.g. `uni005eu5` for `uni005eu5.fusionsolar.huawei.com`. Defaults to `uni005eu5`. |
| `FUSIONSOLAR_PLANT_DN` | Plant DN, e.g. `NE=123456789`. Blank → auto-discover the account's first plant. |
| `FUSIONSOLAR_MAX_STALENESS_S` | Max age (seconds, default `1800`) of the newest *usable* point before the read is reported unavailable — stops a stopped feed from masquerading as live. Generous on purpose; see "Reading around the source's gaps" above. |
| `FUSIONSOLAR_CACHE_TTL_S` | Seconds one cloud response is reused (default `60`). The portal publishes on a 5-minute grid, so polling harder buys no resolution and risks throttling. The PWA polls energy every 5 s, which without this would be a cloud round-trip each. |

Smoke-test the credentials with the CLI:

```powershell
& .\.venv\Scripts\python.exe -m src.list_energy      # Windows
```

```bash
./.venv/bin/python -m src.list_energy                # POSIX
```

`GET /api/energy` serves the same snapshot to the PWA.
