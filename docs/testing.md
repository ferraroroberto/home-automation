# Testing

## Tests

Two suites, both run with the same interpreter and `pytest`. One pre-ship gate
command runs both and routes the browser suite proportionately to the diff:

```powershell
& .\scripts\verify-before-ship.ps1
```

Byte-compiles the repo, runs the static and docs ratchets and the backend suite
unconditionally, then
classifies the branch's changed files against `.fleet.toml`'s `[e2e]` table
(`scripts/classify_e2e.py`, vendored byte-identical from `project-scaffolding`)
and runs a browser slice proportionate to the diff: a backend/docs/meta-only
change skips the browser suite entirely, an inert static asset gets a narrow
Chromium-only smoke check, and any real UI/behaviour change (including a
change to the e2e harness itself) gets the full Chromium + WebKit run. Any
mixed, unmatched, or ambiguous diff fails safe to the full suite — routing
never narrows coverage on uncertainty. On CI the full suite always runs.

The static ratchet (`scripts/static_ratchet.py`, vendored byte-identical from
`project-scaffolding`; `.fleet.toml` `[static_ratchet]`) compares the tree with
the committed `.static-ratchet-baseline.json` and fails on any duplicate block
(jscpd through `npx`, so Node must be installed), unused import/variable
(ruff `F401,F841`) or ruff bug (`B`) / security (`S`) finding (`S113` strict) that
is not already in it. Existing debt is baselined, so the
count can only go down; shrink the baseline in the PR that removes debt with
`python scripts/static_ratchet.py --update-baseline`. A detector that cannot run
(no `npx`, no network on the first `npx`, ruff missing) exits 3, which the gate
reports as NOT CONFIRMED and fails, never as a pass. The gate calls it through
`python -m scripts.static_ratchet_gate`.

The docs ratchet (`scripts/docs_ratchet.py`, vendored byte-identical from
`project-scaffolding`; `.fleet.toml` `[docs_ratchet]`) enforces the README and
docs convention: docs state what is true now and why. It fails when the root
`README.md` passes 40 KiB or its baselined size, and when `README.md` or any
`docs/**/*.md` file holds more history markers than
`.docs-ratchet-baseline.json` records for it: an issue reference, a dated
heading or a changelog heading (fenced code, URLs and link targets do not
count). Existing markers are baselined, so each file's count can only go down;
lower the baseline in the PR that rewords a doc with
`python scripts/docs_ratchet.py --update-baseline`, and prefer a present-tense
sentence to `--allow-additions`. It reads the whole tree and is never
path-routed. Exit 3 (git or a doc unreadable, a fence that never closes) is
NOT CONFIRMED and fails, never a pass; the gate calls it through
`python -m scripts.docs_ratchet_gate`.

CI (`.github/workflows/ci.yml`) runs this same gate on a clean Windows runner
for every PR and every push to `main`: a fresh `.venv` from the requirements
files, only committed files, and each `config/*.sample.json` copied to its live
name. It is advisory (no branch protection); the local gate is the contract.
CI needs no secret, and no test or workflow step reaches a real device, camera,
calendar or push service. A failing run uploads its Playwright traces and
screenshots as an artifact. To reproduce a clean-runner red locally, run the
suite from a checkout that has only the sample config files.

Every gate run writes a per-test progress log: phase markers from the script,
plus a `START`/`DONE (<s>)` line per browser test and a traceback excerpt under
each `FAILED` line from the `tests/_progress_log.py` pytest plugin. The live
run goes to `webapp/verify-progress.run.log` in the checkout running the gate
(it names the active test if a run wedges). Every run that reached the browser
phase is then appended to the **primary** checkout's
`webapp/verify-progress.log`, keeping the last 20, whichever worktree it ran
in. That history is the timing source `.fleet.toml` `[e2e] progress_log`
declares for fleet-config's `/e2e-audit`, which ranks the suite by seconds and
reads its red history from it. Both files are gitignored.

### Backend suite — fast, no network or browser

A Python-level layer under `tests/` (excluding `tests/e2e/`) exercises the real
backend in-process:

- **API smoke** (`tests/api/`) drives `app.webapp.server:app` with FastAPI's
  `TestClient` (presenting as a loopback caller, so the bearer gate is bypassed
  exactly as it is for local probes). It asserts `/healthz`, `/api/version`, and
  `/` answer, and that `/api/units` + `/api/energy` flatten their fetched data —
  with the cloud fetchers **monkeypatched**, so it never calls
  MELCloud Home / FusionSolar / Tuya / Risco.
- **Unit tests** cover the `src/` domain logic, one module per concern —
  presence (client, engine, automation, refresher), the network stack, cameras,
  Tuya, UPS, voice, telemetry, tariff and energy history, `ha_config_sync`,
  push notifications, PV forecast and the rest — driving the real code against
  `tmp_path` fixtures with every cloud fetcher monkeypatched.

Together that is ~1,170 tests across 93 modules. Treat the number
as indicative and re-count rather than trusting it — add `--collect-only -q` to
the run command below.

```powershell
& .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
& .\.venv\Scripts\python.exe -m pytest tests -p no:cacheprovider --ignore=tests/e2e
```

### Browser E2E suite

A Playwright browser-E2E suite lives in `tests/e2e/`. It boots the real
webapp — always its **own disposable instance**, with the energy sampler off;
if the configured port is already taken it *refuses* rather than
adopting whatever is there, unless `E2E_LIVE=1` explicitly opts in (see
`tests/e2e/_e2e_live_guard.py` and CLAUDE.md) — and drives the PWA, **stubbing
`/api/units`, the `/api/energy*` endpoints, and `/api/tuya*` with fixtures** (one module per area under `tests/e2e/fixtures/`) so
it never touches the live cloud, the LAN, or actuates real HVAC. Coverage
includes the Home/AC/Energy/Devices tab navigation, the Home AC summary, an
Energy-tab render (hero numbers + charts), and the Devices tab (metered-plug
watts, a switch round-trip, cover controls, light controls, and an offline
device). Runs in two
projections — Chromium desktop + WebKit on an iPhone 14 (except the Chromium-only
modules below).

Test modules are scoped **one per feature area** — `test_tabs.py` is pure nav
and tab switching, while tab content lives in
`test_home_tab.py`, `test_vm_tile.py`, `test_ac_tab.py`, `test_energy_tab.py`,
`test_security_tab.py`, `test_cameras.py` and `test_presence.py`. Add a
new case to the module named after what it exercises; `tests/e2e/_app.py` holds
the shared `boot_home()` opener the feature modules use.

**Design inventory.** `tests/e2e/_surface.py` is the
vendored `surface(page, "<tab>.<surface>")` call: inert in a normal run, but when
`/design-review home-automation --e2e` (or `measure --e2e`) sets
`DESIGN_SURFACE_CAPTURE` it screenshots and inventories the open surface in both
themes. Call it directly in the test body, after the assertions and any `shot()`,
in the first test that opens each dialog, sheet or state; the name is unique per
app. In capture mode the vendored hook deselects every test without a `surface(`
call. The walk runs on `scripts/design_review_synthetic.py`, a disposable webapp
with the engines off and no credential or LAN access, never the tray; run it from
a checkout whose `config/` holds only the `*.sample.json` twins (the app has no
config-dir override). `pytest tests/e2e --e2e-autoboot` boots that disposable
instance even while the tray owns this checkout's port, which the live-instance
guard would otherwise refuse.

**Screenshot baseline.** A move-only change (a stylesheet split, a markup move) must
not change a pixel, and the gate proves it instead of asking for a look on a phone.
`tests/e2e/test_shot_baseline.py` captures the home, AC, energy and security tabs at
phone (390x844 at scale 3) and desktop (1280x900), light and dark, through the
vendored `tests/e2e/_shot.py` (pinned clock, time zone and locale; Chromium only) into
the committed `tests/e2e/shots/`. The last gate stage, `scripts/shot_baseline.py`
(config: `.fleet.toml` `[shot_baseline]`), compares what the run recaptured with the
committed PNGs, fails on a visible change naming the shots, and restores the tree, so a
verification run leaves no rewritten PNG behind. Exit 3 is NOT CONFIRMED and fails.

- The shots hold fixture data only. The booted app answers some reads from this
  machine's gitignored `config/` or from the internet, so the test stubs those
  (`_MACHINE_STATE`: build version, weather, cameras, reminders, wake alarms, UPS, presence
  automation); a screen that adds such a read adds its stub, or its shot would carry
  personal data into the public repo.
- The baseline is this machine's rasteriser, so the gate skips the comparison on CI.
- A drift is either an intended UI change (re-baseline) or something unpinned on screen
  (fix the capture). Never widen a band or allowlist a shot to get green.
- Re-baseline a deliberate UI change in the same PR: run
  `& .\.venv\Scripts\python.exe -m pytest tests/e2e/test_shot_baseline.py --browser chromium`,
  review `git diff --stat -- tests/e2e/shots` and the moved images, then
  `git add tests/e2e/shots` and commit them with the change.

A full run executes 220 nodes in ~3.1 min (3m06s, run-to-run spread 2m55s–3m20s):
282 are collected and 62 WebKit nodes are deselected. `test_design_matrix.py` additionally fans one function across
the viewport × theme matrix below. CLAUDE.md tracks this as a runtime budget,
not a fixed test-count cap.

Modules named in `CHROMIUM_ONLY_MODULES` (`tests/e2e/conftest.py`) run on
Chromium only: they assert DOM state, request wiring or copy, with no layout,
touch-target, nav, safe-area or phone-width check. Add a module there only when
that holds for every test in it; a module with even one layout test, and the
boot canary and login, stay on both projections.

A module that mixes DOM-state tests with a layout test (`test_network.py`,
`test_presence.py`) marks the DOM-state ones with `@pytest.mark.chromium_only`
instead: the same deselection, applied per test. A test that checks
geometry, touch targets, overflow, phone width or engine-specific behaviour
(the streamed voice recorder in `test_home_assistant.py`) keeps WebKit.

To make a panel's loading state observable, hold its read with
`tests/e2e/_app.py`'s `hold_reads()` and release it once the loading state is
asserted. Don't add a fixed delay: every test would sleep through it, and a
loaded machine can outrun it. To test anything that waits on a poll interval,
drive it with `page.clock` instead of waiting it out.

Rendered-geometry design checks (44px effective touch targets, non-overlap,
horizontal overflow, live Chart.js tick/cue config) go through
`tests/e2e/_geometry.py` — vendored **byte-identical** from
`project-scaffolding/tests/e2e/_geometry.py` (like `app/tray/single_instance.py`;
never fork it, upstream changes first). `tests/e2e/test_design_matrix.py` runs
the helper's 320/390/430/772px × light/dark matrix against the home view,
driving the theme through the app's own `home-automation.theme` localStorage
boot path with a per-leg check that the theme actually applied.

Leaked browser helpers are reclaimed by `tests/e2e/_browser_sweep.py` — vendored
**byte-identical** from `project-scaffolding/tests/e2e/_browser_sweep.py` (never
fork it, upstream changes first), wired as `pytest_sessionfinish` in
`tests/e2e/conftest.py` with this checkout as the only call-site argument. After
every session it classifies each WebKit helper process and tree-kills only those
that are really running, genuinely orphaned (dead parent, checked against PID
reuse) **and** whose working directory sits under this checkout — the leak that
otherwise pins the directory and makes a later `git worktree remove` fail as an
unexplained "busy". Everything else gets its own verdict instead of a kill: an
already-exited-but-handle-held `zombie` (unkillable, harmless, never a gate
failure), a live-parent session, a sibling checkout, or an unreadable cwd.
Killing by image name alone stays forbidden, so Chromium is deliberately out of
the sweep set. The run prints a one-line `[e2e] browser sweep (scope …): …`
verdict breakdown; it can also be run standalone before removing a worktree:

```powershell
& .\.venv\Scripts\python.exe tests\e2e\_browser_sweep.py E:\automation\home-automation-wt-1 --dry-run
```

The sweep's `wedged:out-of-scope` count is host-wide and cumulative: it holds
every earlier run's unkillable residue. So the session also snapshots the
wedged helpers at start and prints `[e2e] browser helpers newly wedged during
this run: N` (`UNKNOWN` when the state can't be read). The gate repeats it as
its own `[INFO]`/`[WARN]` line, advisory and never part of the PASS.
That line, the sweep summary and any teardown-watchdog firing
(`teardown watchdog fired: browser.close() [webkit] …`) all land in the
progress log, so a wedge can be diagnosed from the gate log alone. The count
is host-wide, so a concurrent gate in another checkout can add to it.

**Read that `zombie` count as "not inspected", not "harmless".** The
sweep infers "already exited" from `GetExitCodeProcess`, and a helper
force-killed by the teardown watchdog can wedge *inside* termination: the exit
code is set, so `taskkill` answers "no running instance" — but `GetProcessTimes`
reports no exit time, a thread is still alive, and the **cwd handle is still
held**. Such a helper is unkillable *and* still pinning whatever directory it
ran from, which leaves empty, undeletable worktrees. So
the sweep is defence in depth, not the protection:

- **The protection** is that the Playwright driver is started from `%TEMP%`
  (`_neutral_driver_cwd` in `tests/e2e/conftest.py`). Every browser and helper
  inherits the driver's working directory, so none of them ever roots in this
  checkout and a wedged one pins a directory nobody wants to delete.
  `tests/e2e/test_helper_cwd_isolation.py` pins that arrangement.
- **The remedy**, if something pins a directory anyway, is
  `scripts/unpin_directory.py` — no reboot needed. It closes the wedged
  holder's cwd handle, and refuses outright when the holder is alive:

  ```powershell
  & .\.venv\Scripts\python.exe scripts\unpin_directory.py E:\automation\home-automation-wt-1
  & .\.venv\Scripts\python.exe scripts\unpin_directory.py E:\automation\home-automation-wt-1 --close
  ```

```powershell
& .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
& .\.venv\Scripts\python.exe -m playwright install chromium webkit
& .\.venv\Scripts\python.exe -m pytest tests/e2e                       # both projections
& .\.venv\Scripts\python.exe -m pytest tests/e2e --browser chromium    # faster dev loop
```
