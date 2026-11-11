# Declared state machines

Each lifecycle in this repo has one declared state type and one derive function in an owner module, listed under `[[state_machines]]` in `.fleet.toml`. Readers ask the derived value; none re-derives it from a raw status word. The conventions ratchet's `state-literal`, `state-set-repeat` and `state-write` rules report any other module that compares or stores a lifecycle word as a state, so a new reader has to call the owner.

| Machine | Owner | States | Derived from | Readers |
|---|---|---|---|---|
| `view-state` | `app/webapp/static/view-state.js` (`ViewState`, `deriveViewState`) | `idle`, `loading`, `ready`, `empty`, `error`, `stale`, `hidden` | the outcome of a fetch (`fetched`, `restored`, `failed`) plus whether the body held data and whether last-good data is on screen | every tab controller, `renderFeedback`, `createTabLoader` |
| `searxng-phase` | `src/voice/searxng_client.py` (`SearxngPhase`, `derive_searxng_phase`) | `serving`, `unreachable`, `down`, `not_found` | Docker's container status word and the `/healthz` probe | the watchdog's recovery plan, `GET /api/searxng` (`phase`), the Settings row |
| `vm-phase` | `src/devices/hyperv_client.py` (`VmPhase`, `derive_vm_phase`) | `running`, `off`, `transitioning`, `not_found`, `unknown` | Hyper-V's lower-cased `State` word | the power toggle guard, `GET /api/hyperv` (`phase`), the Home Assistant card |
| `vehicle-state` | `src/devices/tesla_client.py` (`VehicleState`, `TeslaReading.awake`) | `online`, `asleep`, `offline`, `unconfigured`, `error` | Tesla's connectivity word, or the read's own failure | the sampler's cadence, `GET /api/tesla` (`awake`), the Tesla card |
| `daily-fire-latch` | `src/_schedule_store.py` (`DailyFireLatch`) | per entry key: the local date it last fired | the persisted `{key: date}` file | every schedule engine |

## Rules the machines keep

- `view-state`: nothing moves into `idle` (the starting state) and a name outside the set is refused with a console warning, leaving the state as it was. The string values are the `data-state` attribute values the stylesheets select on.
- `searxng-phase`: a stopped or missing container is never `serving`, whatever the probe says. A restart is planned only from `unreachable`, only past the grace window. Docker's status word stays on the state as `container_status` for display.
- `vm-phase`: a start on a `running` VM and a stop on an `off` one are refused before any cmdlet runs. Hyper-V's word stays on the state as `state` for display.
- `vehicle-state`: only `online` is awake. A word Tesla adds later is treated as not awake until it is declared. The sample's `charging` flag is derived server-side from the charging state.

## Wire fields

The derived values reach the browser as `phase` (searxng, hyperv), `awake` (tesla) and the sample's `charging`. The browser reads those fields and never the raw words, except to show them.

## Proof

Each machine has a test carrying a `state-machine-proof: <name>` marker that drives an illegal move and asserts it is refused: `tests/state_machines/test_view_state_machine.py` (runs the real `view-state.js` under Node with a stubbed DOM) and `tests/state_machines/test_declared_state_machines.py`. No test touches Docker, Hyper-V or Tesla: the derive functions are pure and the action paths are monkeypatched to fail if reached.
