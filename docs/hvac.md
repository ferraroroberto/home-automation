# HVAC automation and unit names

## Custom unit names

Each HVAC unit has a factory name supplied by MELCloud (e.g. "MSZAP15VGK").
You can override it with a friendlier label — "Living Room", "Master Bedroom" —
that is shown on the unit's rows and in its sheet instead of the API name.

- Open the unit's sheet → **Name** → fill in the **Display name** field → the
  label is saved on blur (or Enter) via `PUT /api/units/{id}/display_name` and
  reflected on the rows without a page reload.
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

The unit sheet has two optional automation pages. The rule saves as it changes
(a switch on tap, a number on blur); schedules are a dense collection whose Add
and Edit open a staged editor, where Save is the only persistence boundary:

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

**Known issue — night idle-jump correlates with musty smell + buzzing.**
One unit intermittently produces a stale/musty (humidity-like) smell and a
buzzing noise at night that only stops once the setpoint is manually touched.
Likely not a control-logic defect: the idle jump above is by design
(specifically to avoid power-cycling the compressor), and the symptoms line up
with mechanical/moisture behavior *during* that idle state — an inverter
compressor/EEV hunting at minimum modulation (buzz, which any setpoint change
resolves by forcing the valve to reposition) and a coil that isn't cold enough
to actively condense/drain while the fan still moves air across it (musty
smell). Recommended next step is a physical inspection of the unit's filter,
coil, and condensate drain, not a software power-cycle override — reversing
that tradeoff wouldn't address a wet coil and risks compressor wear for no
benefit.

Rules and schedules are evaluated server-side by the tray-owned webapp, so they
work while the PWA is closed. Runtime files live in gitignored
`config/hvac_rules.json` and `config/hvac_schedules.json`; committed samples show
the shape. Existing single-schedule config files are loaded as one-entry lists. Optional `.env` knobs:

| Key | Default | Meaning |
|-----|---------|---------|
| `HVAC_AUTOMATION_ENABLED` | `true` | Master switch. Set `false`/`0` to disable the evaluator. |
| `HVAC_POLL_INTERVAL_S` | `60` | How often the evaluator checks configured rules/schedules. With no config it does not hit MELCloud. |
| `HVAC_ADJUST_INTERVAL_S` | `900` | Minimum time between dynamic setpoint nudges per unit. |
| `HVAC_BUFFER_C` | `0.5` | Room-temperature hold band around the rule target. |
| `HVAC_BOOST_SURPLUS_ON_W` | `1500` | PV surplus (watts) that starts a solar boost on eligible units. |
| `HVAC_BOOST_SURPLUS_OFF_W` | `500` | PV surplus (watts) below which a boost is eligible to end — the lower half of the hysteresis band. |
| `HVAC_BOOST_MIN_DURATION_S` | `1800` | Minimum time a boost holds once started, regardless of surplus, to avoid thrashing. |

Solar-surplus boost: a per-unit opt-in (`boost_enabled` + `boost_offset_c`, in the unit sheet's Temperature rule page) that shifts the rule's own steered target further toward comfort while PV surplus is high, then lets it relax back via the same gradual steering law once surplus drops — pre-cooling/heating to bank thermal inertia ahead of a PV drop. Inert unless the unit's temperature rule is also enabled (boost shifts that rule's target). Start/stop transitions are logged to the Activity log (`domain=hvac`, `boost_start`/`boost_stop`); a unit currently boosted shows an accent chip on its row with the signed offset actually applied — e.g. `Boost -2` while cooling, `Boost +2` while heating.

**Boost is sequenced across units, one at a time.** With several eligible units, one shared surplus reading and one shared threshold is an oscillator, not a controller: every unit would enter boost in the same tick, their combined draw can exceed the surplus and push the house into import, and — because they all started together — they would become sheddable together, shed together, and repeat. The engine therefore admits **at most one unit per settle interval**, then re-reads the *measured* surplus (which now contains that unit's real draw, so no per-unit power model is needed) before considering the next. As surplus declines, units leave one per interval in **reverse admission order** (last admitted, first shed); a unit still inside its `HVAC_BOOST_MIN_DURATION_S` is skipped rather than jumped ahead of. Sustained **import** past the fast-shed threshold is the one exception: it drops every boosted unit at once and overrides the min-duration debounce, because otherwise a herd-induced deficit would persist for the full 30 minutes. It sheds all rather than one-per-tick deliberately — there is no per-unit power model to size a partial shed with, and the solar feed runs several minutes behind, so shedding one per tick would re-decide repeatedly against the same stale reading while the house keeps importing.

On admission (and symmetrically on shed) the unit's **actual setpoint is commanded in that same tick**, computed straight from the boosted target rather than the usual single 0.5 °C step — otherwise a 2 °C boost would express as ~0.5 °C per 15 minutes and take about an hour to arrive, so a sequencer measuring 5 minutes later would see no added draw and admit the whole fleet before any of it materialised. That immediate write is scoped to the transition only: a unit that is already boosted goes straight back to the normal one-step-per-`HVAC_ADJUST_INTERVAL_S` steering law. It is a **setpoint write only** — the coordinator never toggles power, so the compressor is never cycled. Every decision leaves a breadcrumb in the Activity log with a distinct reason: `admitted` on `boost_start`, `shed_sequential` / `shed_deficit` on `boost_stop`, and `boost_hold` / `held_margin` when a candidate is waiting for headroom (logged once per hold episode, not once per poll).

The coordinator knobs are **fleet-wide** and edited from **Settings › Energy › Solar boost** — persisted to gitignored `config/hvac_boost.json` (committed `…sample.json` shows the shape) and re-read every tick, so a change is live with **no tray restart**. The per-unit `boost_enabled` / `boost_offset_c` stay per unit in the unit sheet; rooms are deliberately opted in one by one and that never becomes a global policy. The `HVAC_BOOST_*` hysteresis knobs above are `.env`-only (they are read once at start-up).

| Key (`config/hvac_boost.json`) | Default | Meaning |
|-----|---------|---------|
| `settle_interval_s` | `300` | Minimum seconds between two changes to the boosted set. **Floor 300**, ceiling 3600 — FusionSolar publishes on a 5-minute grid, so a shorter interval re-reads the same value and admits again before the last admission has shown up in it. The interval also has to cover the compressor's own ramp-up. |
| `admission_margin_w` | `0` | Extra watts of measured surplus, on top of `HVAC_BOOST_SURPLUS_ON_W`, required to admit the *next* unit. `0` still means "the full entry threshold is measurably spare", which is a real test because the reading already contains what the boosted units draw. |
| `hard_deficit_w` | `1000` | Sustained import (positive watts) at which every boosted unit is shed at once. |

API: `GET`/`PUT /api/hvac/boost-coordinator`.
