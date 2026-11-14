# Home scenes

The Home tab's **Scenes** card turns the chores that take several taps into one row each: **Leave**, **Arrive** and **Night**. A tap opens one confirm dialog that names every step, because a scene moves several device families at once. On **Run**, the PWA calls `POST /api/scenes/{id}`.

## What each scene does

| Scene | Blinds | AC | Alarm |
| --- | --- | --- | --- |
| Leave | down | every running unit off | full |
| Arrive | up | unchanged | off |
| Night | down | unchanged | perimeter |

Those are the built-in defaults in `src/devices/home_scenes.py`. To change them, copy `config/home_scenes.sample.json` to `config/home_scenes.json`, which is gitignored, and edit it. Each scene can override any of `label`, `blinds` (`open` / `close` / `null` = leave alone), `ac_off` (`true` / `false`) and `alarm` (`disarm` / `arm` / `partial` / `perimeter` / `null`). The file is read on every request, so an edit applies without a restart. An invalid value keeps that field's default and logs a warning, so a typo can never make a scene do something its row does not say. The three scene ids are fixed because the rows are drawn at first paint.

## How a run works

`app/webapp/routers/scenes.py` calls the same functions the per-device controls call, so a scene has their side effects too:

- **Blinds:** `move_blinds()` on every blind, then `settle_manual_move()`. Blinds that fail are retried in the background, exactly like the Devices tab's *All up / All down*.
- **AC:** `set_device_state(power=False)` on each unit the units snapshot reports as on. Units already off get no command.
- **Alarm:** `perform_manual_alarm_action()`, the same path as the arm control. The activity log records it with actor `scene:<id>`, the presence engine sees a manual action, and a disarm triggers the blind follow-up.

The blinds and AC steps run in parallel. The alarm step runs last, so the house is closed up before it arms. A failed step does not stop the others. Each step reports `ok` (false only when nothing in that step took) and an `error` when any part of it failed. The response's `ok` is true only when no step has an error, and the request is a 502 only when every step failed. Each run is recorded as a `scene` event in the telemetry log.

`GET /api/scenes` returns each scene's label and its steps as short phrases. These fill the rows' meta lines.

## Testing

Nothing in the test suites actuates a device. `tests/api/test_scenes_api.py` fakes the blinds, the units and the alarm. The browser suite stubs `/api/scenes` for every test (`tests/e2e/fixtures/stubs.py`), and any unstubbed POST answers 503. A live run on the real house is an owner check.
