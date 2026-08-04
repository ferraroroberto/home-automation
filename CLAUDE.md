# Project Instructions

Canonical instructions for AI coding agents working in this repository. Claude Code reads this file directly as project memory. Other agents (Cursor, Codex, etc.) reach it via the one-line `AGENTS.md` pointer.

> Universal dev-workflow directives (plan mode, asking, before/while editing, git, branch & PR pipeline, documentation discipline) live once in the machine config (`~/.claude/CLAUDE.md`) and are not restated here. This file owns only what is specific to this project's shape.

## Streamlit conventions
*Apply only to the Streamlit **spike** (`spike/streamlit_app.py`) — the product is the FastAPI + PWA webapp under `app/webapp/`, not Streamlit.*

- `st.set_page_config(layout="wide", page_title="...")` MUST be the first Streamlit call.
- Use `width="stretch"` (and `width="content"` where appropriate) in new and modified code. **Never** introduce new `use_container_width=True` — it is deprecated. When you touch existing code that uses `use_container_width`, migrate it.
- All mutable state in `st.session_state`. No module-level globals.
- `@st.cache_data` for DataFrames/files; `@st.cache_resource` for DB clients/models.
- Every widget needs a stable, explicit `key=`.
- UI code only in the UI directory (e.g. `app/`). Data logic stays in the non-UI package (e.g. `src/`). Never import `streamlit` from non-UI code.
- User feedback via `st.error()` / `st.warning()` / `st.success()`, not `st.write()`.
- **App layout:** the main file (`app.py`) handles only page config, shared state, the sidebar, and routing. Use `st.tabs()` for sub-sections within a view, and a sidebar radio only when asked.

## End-to-end UI testing
*Apply only if this project serves a browser UI (Streamlit, FastAPI, Flask, etc.).*

Two loops, kept deliberately separate. Don't conflate them. Full reasoning, setup, and bootstrap recipe in [`docs/playwright-ui-testing.md`](docs/playwright-ui-testing.md).

### Iterative verification (headed, agent-driven)
Use this during active development so I can watch the agent verify a change.

- Drive the running app via the **Playwright MCP server in `--headed` mode** (Claude Code, Codex CLI). For tools without MCP support, fall back to a small `playwright` Python script run via Bash with `headless=False` — same shape, just less ergonomic.
- Boot the app **once** on a fixed port (Streamlit default: 8501) and leave it running. Do NOT restart between iterations unless `set_page_config` or top-level imports changed.
- Prefer the a11y `snapshot` tool over `screenshot` — DOM is far cheaper than pixels in tokens. Screenshot only on failure or as final visual confirmation.
- Cap actions per cycle in the prompt (≤ 5 actions, then report). Stop and ask if the page state is unexpected; do not loop blindly.
- Target widgets via their stable `key=` using `page.get_by_role(..., name=...)` or `page.get_by_test_id(...)`.
- Do NOT create files under `tests/e2e/` for verification — it's throwaway, lives in the conversation only. Promotion to a permanent test is a separate, deliberate decision (see below).

### Regression suite (headless, pytest-playwright)
Optional. Lives at `tests/e2e/`. **Don't create the folder until the first regression test is actually justified.**

- Add a test only when all three hold: (1) silent breakage would hurt, (2) it can't be caught by a unit test under `tests/`, (3) the behavior has stabilized (not still in flux).
- Runs via `& .\.venv\Scripts\python.exe -m pytest tests/e2e/` (Windows) / `./.venv/bin/python -m pytest tests/e2e/` (POSIX). No LLM in the loop, zero per-run cost.
- **One shared session fixture boots the app once per pytest run.** Boot on a fixed or free port; **adopt** an instance already listening rather than spawning a second.
- **Boot failure is a hard failure — never `pytest.skip`.** A regression suite that skips when the app isn't up reports green on a build it never tested.
- Budget by runtime, not test count — add coverage whenever (1)–(3) above hold; there is no fixed cap on test count. No Page Object Model. Don't gate commits on e2e.
- **Local runtime contract (measured 2026-07-17, #464): 208 executions (96 test functions, dual Chromium + WebKit projection) in ~4 min (3m55s) on an idle dev box.** Investigate if a full run exceeds ~8 min.
- When you remove a feature, remove its e2e test in the same commit.

## UX surface
*The design-conformance gate the `/issue-{start,finish,yolo}` skills read (convention: `project-scaffolding#83`). This is a live, parseable block — the product is the FastAPI + static PWA under `app/webapp/`.*

- design spec applies: yes        # `no` would make the gate a permanent no-op; this repo serves a real PWA
- paths:
  - app/webapp/static/**/*.css
  - app/webapp/static/**/*.{js,html}
- key views:                      # the app is a single tabbed SPA served at `/`
  - /          (home — card grid, energy tile, bottom nav)

## Verification (before declaring a task done)
Windows / PowerShell:
- Syntax: `& .\.venv\Scripts\python.exe -m py_compile <file>`
- CLI smoke: `& .\.venv\Scripts\python.exe -m src.list_devices` (HVAC) · `& .\.venv\Scripts\python.exe -m src.list_energy` (FusionSolar energy) · `& .\.venv\Scripts\python.exe -m src.list_circuits` (Athom per-circuit clamps)
- Webapp boot check: `& .\.venv\Scripts\python.exe -m uvicorn app.webapp.server:app --host 127.0.0.1 --port 8447` then `curl -k https://127.0.0.1:8447/healthz`, `…/api/units` and `…/api/energy` (loopback bypasses the token).
- Streamlit spike boot check: `& .\.venv\Scripts\python.exe -m streamlit run spike/streamlit_app.py --server.headless true`

**Pre-ship gate — one command, routes itself:** `& .\scripts\verify-before-ship.ps1`. Runs byte-compile + the backend suite (`tests -p no:cacheprovider --ignore=tests/e2e` — API smoke via FastAPI `TestClient` under `tests/api/` + pure-logic unit tests for `src.tariff` / `src.energy_history` / `src.display_names`; cloud fetchers are monkeypatched, nothing touches MELCloud/FusionSolar/Tuya/Risco) **unconditionally**, then routes **only** the browser phase: `scripts/classify_e2e.py` classifies the branch's changed files against the `.fleet.toml` `[e2e]` rules and runs a slice proportionate to the diff — `skip` (backend/docs/meta-only), `static` (an inert asset — the narrow smoke target, Chromium only), or `full` (real UI/behaviour — the whole `tests/e2e` suite, Chromium + WebKit). Fail-safe to `full` on any mixed, unmatched, or ambiguous diff (ferraroroberto/home-automation#603, project-scaffolding#180). On CI (`$env:CI -eq "true"`) routing is bypassed and the full suite always runs. Install the test deps first: `& .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt`.

Run the browser suite directly when iterating on it: `& .\.venv\Scripts\python.exe -m pytest tests/e2e` — boots its own disposable instance by default (issue #538); runs Chromium + WebKit projections. Faster dev loop: `--browser chromium`. If the tray's port (:8447) is occupied and you want the suite to run against it instead of booting a second instance, set `E2E_LIVE=1` — read-only adoption only, guarded by the vendored `tests/e2e/_e2e_live_guard.py` (a bare run with the port occupied and no flag refuses rather than silently driving the tray). `[e2e]` table schema reference: `project-scaffolding/docs/e2e-routing.md`; anti-drift guard: `tests/test_classify_e2e.py`, which loads the real `.fleet.toml` and must be updated in the same PR as any new e2e-relevant directory.

Report failures with the output rather than claiming "tests pass."

## Internal architecture

[`docs/architecture.mmd`](docs/architecture.mmd) is a hand-authored Mermaid diagram of this repo's own internal structure (the device clients in `src/`, the FastAPI + PWA product in `app/webapp/`, the tray, the Home Assistant integration, and the ops scripts) — the per-repo companion to the fleet-wide convention established in `fleet-config#256`. Update it in the same PR as any material structural change (a new device integration, a relocated automation task, a moved script) — same anti-staleness contract as this repo's own `.fleet.toml` `description` field. It is not auto-generated and not covered by the verification gate; see [`docs/architecture.md`](docs/architecture.md) for the exhaustive per-file inventory.

## This repository
Proof-of-concept for reading and controlling Mitsubishi Electric HVAC units, ahead of a **solar load-balancing automation** (the eventual goal: shift HVAC load to match PV generation — see the sister `pvgis` repo for the solar-output estimate side).

**Platform:** MELCloud Home (a different API from classic MELCloud) via [`aiomelcloudhome`](https://github.com/erwindouna/aiomelcloudhome); MELCloud Home credentials go in `.env`. Full rationale — why classic `pymelcloud` sees zero devices — is in README "Platform note" (the single canonical home; don't restate it here).

**Layout:** *(the agent-facing map — directory roles + the conventions worth knowing. The exhaustive per-module inventory has one home: [`docs/architecture.md`](docs/architecture.md), also linked from the README — don't grow the per-file catalogue here or in the README.)*
- `src/melcloud_client.py` — async auth + fetch + control (the shared, UI-free core). `fetch_devices()` walks buildings → air-to-air units; `set_device_state()` writes via `control_ata_unit`. Capabilities drive the selectable modes, fan speeds, per-mode temperature bounds, and the two vanes (vertical/horizontal).
- `src/list_devices.py` — CLI that prints each unit's live state.
- `src/huawei_client.py` — async, UI-free read of the **Huawei solar/energy** flow (issues #21, #535, #618): PV production, house consumption and the grid exchange measured by the SUN2000's RS485 power sensor. `fetch_energy_state()` returns the flattened `EnergyState` (grid/PV/consumption/net); missing credentials, an outage or a stale upload give an empty state with `*_reachable=False`, never an exception. Since #618 it tries `src/huawei_modbus.py` **first** and falls back to the **FusionSolar cloud** path below on any failure, logging the serving source whenever it changes. The cloud stays the sole source for `fetch_energy_day()` (history backfill — the registers are instantaneous) and for today's cumulative kWh counters (the meter's own registers are *lifetime* totals, which would corrupt HA's `TOTAL_INCREASING` "energy today" sensors). FusionSolar reports one **signed** `meterActivePower` (**positive = importing**, the opposite of the portal's device page) which is split into `grid_import_w`/`grid_export_w` in exactly one place. The day arrives as parallel 5-minute series whose newest bucket may be half-written *without* being marked `--`, so a bucket is only accepted when the flow identity `productPower + meterActivePower == usePower` holds (±5 W); otherwise the read steps back. One cloud response is cached for `FUSIONSOLAR_CACHE_TTL_S` (default 60 s) and shared by the tile, the sampler and the backfill — the source only publishes every 5 min. Wraps `fusion_solar_py`.
- `src/huawei_modbus.py` — async, UI-free **local** read of the same flow over Modbus TCP through the **SDongleA-05** smart dongle (issue #618): ~1 s resolution against the cloud's 5-minute grid. `fetch_modbus_state()` returns `None` for every failure mode (unconfigured, unreachable, moved, rebooting) so the caller falls back cleanly; it never raises. Three things worth not breaking. Register **`37113` is positive when *exporting*** — the **opposite** of the cloud's `meterActivePower`, proven against live hardware (see the module docstring) — so the two sources each keep their own single split site and must **never** share a helper. PV comes from **`32080`** (inverter AC output), not `32064` (DC input), so the flow balances at the meter instead of overstating house load by the conversion loss. And the dongle tolerates **one client at a time** — a second session drops the first mid-read — so reads are lock-serialised, the session closes every cycle, one snapshot is reused for `HUAWEI_MODBUS_CACHE_TTL_S` (default 5 s) to hold the on-device cadence down, and this app must be the sole collector (Home Assistant reads `/api/energy`, never the dongle). It holds a DHCP lease and **moves**, so `HUAWEI_MODBUS_HOST` is a cold-start hint only and a failed read re-resolves it by MAC via `src.network_client.resolve_ip_by_mac`, reusing `camera_client.py`'s pattern.
- `src/list_energy.py` — CLI that prints the live energy flow (mirrors `list_devices.py`), and at `INFO` which of the two sources served it.
- `src/athom_client.py` — async, UI-free read of **per-circuit** power from Athom BL0906 CT-clamp meters (issue #25) — the complement to `huawei_client.py`'s whole-home total. `fetch_circuits_state()` returns the flattened `CircuitsState`. Meters are **discovered over mDNS** (`_esphomelib._tcp.local.`, narrowed to Athom energy monitors by their advertised `package_import_url` / `project_name`, which excludes the household's Voice PE satellites on the same service type), so a new meter needs no registration anywhere; `ATHOM_METER_HOSTS` is the static fallback. Reads are **one SSE snapshot** off ESPHome's `/events` rather than 21+ per-sensor REST calls. Two invariants worth not breaking: **every channel the meter has is returned whether or not a clamp is fitted** (a clamp added later must just start reading), and an **empty mDNS sweep never deletes a known meter** — measured 1 miss in 20 cold browses, so "found nothing" is unproven, not fact. `src/circuit_prefs.py` holds the per-channel label, the `invert` flag that corrects a clamp fitted backwards (the BL0906 reports signed power), and the `hidden` flag that puts an unused terminal away in the card (#619); it is keyed `"<meter_id>:<channel>"` — and the bare meter id for the meter itself — and is deliberately *not* another flat `display_names.py` clone because a channel carries several facts, not one. Hiding is presentation only and always a user decision: the API still returns every channel, and the founding invariant that nothing is dropped for reading 0 W is unchanged. `src/list_circuits.py` is the CLI.
- `src/webapp_config.py` — webapp host/port + auth secrets (`auth_token` / `auth_password`); real `config/webapp_config.json` gitignored, `…sample.json` committed.
- `src/display_names.py` — maps unit IDs → custom display-name overrides; persisted atomically to `config/display_names.json` (gitignored, `config/display_names.sample.json` committed). `load_display_names()` / `save_display_names()` / `set_display_name()` — edited via the detail-modal "Display name" input and `PUT /api/units/{id}/display_name`. `src/tuya_display_names.py` (plugs) and `src/security_display_names.py` (RISCO detectors, keyed by zone id) reuse this module's atomic load/save/set verbatim — only the on-disk path differs (`config/tuya_display_names.json`, `config/security_display_names.json`; both gitignored with committed `…sample.json`) — so the three rename stores never diverge.
- `src/risco_client.py` — async, UI-free RISCO Cloud alarm core. `fetch_security_state()` returns the flattened `SecurityState` (system flags incl. **`ac_lost`** + per-zone `trouble`, partitions, zones); arm/disarm/bypass go through the native WebUI command path. The cloud API exposes **no per-detector battery** — only a generic per-zone `trouble` boolean (issue #84); the aggregate low-battery flag was removed in #227 as an unreliable proxy.
- `app/webapp/` — **the product**: FastAPI (`server.py` + `middleware.py` + `routers/`) over the same core, serving a static PWA (`static/`). `GET /api/units` → `fetch_devices()`; `POST /api/units/{id}` → `set_device_state(...)`; `GET /api/energy` → `fetch_energy_state()`. Card grid with inline controls + a top energy-flow tile; per-unit detail modal for mode + vanes. `manager.py` (adopt-or-spawn / restart / stop for uvicorn, reading host/port from `webapp_config`) lives here too — at the **canonical fleet path** `app.webapp.manager` so the `projects.toml` `restart_cmd` is identical to every other tray-owned app.
- `app/tray/` — the **Windows tray** that owns the webapp lifecycle (`tray.bat` → `python -m app.tray`). `tray.py` (pystray icon + menu), `__main__.py` (entry); the tray imports `WebappManager` from `app.webapp.manager`. `single_instance.py` + `tray_lifecycle.ps1` are **vendored verbatim** from `project-scaffolding` — never edit per-app.
- `custom_components/home_automation_app/` — Home Assistant custom integration (#235): a thin, reusable adapter over the existing FastAPI `/api/*` endpoints. Exposes HVAC as `climate`, Tuya plugs as `switch`, RISCO as `alarm_control_panel` + zone `binary_sensor`, and FusionSolar flow as `sensor`. It must not import `src.*` or duplicate device logic; HA is just another API client. Live deployment docs: `docs/home-assistant-integration/README.md`.
- `scripts/` — `gen_tailscale_cert.py` (HTTPS via `tailscale cert` — a real Let's Encrypt leaf for the tailnet `.ts.net` name; `--check` auto-renews when within 30 days of expiry), `gen_token.py` / `set_password.py` (auth), `gen_icons.py` (PWA icons; Pillow dev-only), `ha_config_sync.py` (#243 — deploy the repo-owned voice-PE HA config into the HA VM's `/config` over SSH via the Terminal & SSH add-on: `preflight`/`deploy`/`rollback`/`probe`, managed-block replace in `configuration.yaml`, timestamped remote backups, `ha core check`, guarded `--restart`; HA secrets stay live-only — only key **names** are verified, never values).
- `spike/streamlit_app.py` — the **independent POC spike** (throwaway data/debug view; shares only `src/melcloud_client.py`), launched via `launch_app.bat` on :8501.

**Interacting with the Home Assistant VM — SSH first, browser only as fallback (#243).** The SSH shell is a full administrator shell (`root` inside the add-on container, with the `ha` CLI and `/config` mounted). Connection settings live in `.env` (`HA_SSH_*`, `HA_URL`, `HA_TOKEN`). The one-time SSH bootstrap is in `docs/voice-pe-config/README.md`. HAOS host SSH on `:22222` is break-glass only — not the path here.

Reach each task via SSH in this order of preference:

- **Repo-owned voice-PE config** (`configuration.yaml` managed block, `custom_sentences/`) → `scripts/ha_config_sync.py preflight / deploy [--dry-run] [--restart] / rollback / probe`. Idempotent, validates with `ha core check`, takes backups.
- **One-off config edits / `ha` CLI** → `ssh -p <HA_SSH_PORT> <HA_SSH_USER>@<HA_SSH_HOST>` then use the `ha` CLI: `ha core check`, `ha core restart`, `ha core reload`, `ha addon list`, `ha addon install <slug>`, `ha addon start/stop/restart <slug>`, `ha addon options <slug>` (read/write add-on config JSON).
- **File deployment** (e.g. add-on model files, arbitrary `/config` edits not covered by `ha_config_sync.py`) → `scp -P <HA_SSH_PORT> <local_file> <HA_SSH_USER>@<HA_SSH_HOST>:<remote_path>`. Add-on data directories live at `/addon_configs/<slug>/` and `/addon_local/<slug>/` inside the shell.
- **HA REST API** (read entity state, call a service, set a select entity) → `curl -H "Authorization: Bearer <HA_TOKEN>" http://<HA_HOST>:8123/api/...`. Use this to set ESPHome select entities (e.g. wake word picker) without touching the browser: `POST /api/services/select/select_option` with `{"entity_id": "select.<name>", "option": "<value>"}`.

**When the browser IS genuinely needed** (rare — document it in the issue if you hit one): initial ESPHome device adoption, re-flashing ESPHome firmware from the ESPHome dashboard, and any HA onboarding wizard step that has no API/CLI equivalent. Do **not** drive the HA File editor, Developer Tools, or add-on store through Chrome automation when the above paths work — Chrome is the documented fallback for when the Terminal & SSH add-on is down or a key is not provisioned.

**Credentials & secrets:** `MELCLOUD_EMAIL` / `MELCLOUD_PASSWORD` in `.env` (the MELCloud Home login). The repo is **public** — never commit credentials, the bearer token / password (`config/webapp_config.json`), the TLS keys (`webapp/certificates/`), or unit/room names. All are gitignored.

**Security model:** webapp binds `0.0.0.0:8447`, reached over Tailscale behind a real Let's Encrypt HTTPS endpoint + an optional bearer token — the agent-critical part is that **loopback bypasses the token** (local `curl`/tests need no auth; remote needs `Authorization: Bearer` or `?token=`). Full model — cert provisioning, the trusted `.ts.net` URL, the no-cloudflared / no-passkey rationale — has one canonical home: README "HTTPS (Tailscale cert)".

**Restart recipe:** the webapp is owned by the **tray** (`tray.bat` → `python -m app.tray`, on :8447, HTTPS when `webapp/certificates/cert.pem` is present). No hot-reload across imported-module changes, so after editing `src/` or `app/`, **`tray.bat --restart`** — it kills the old tray subtree, orphan-proof-reclaims `:8447` (scoped to this repo's `.venv` by CommandLine), and starts a fresh tray. The signal that new code is live is the served build identity: `GET /api/version` returns `{git_sha, built_at}` and the PWA shows a `Build: <sha> · <ts>` footer — the `git_sha` must match `git rev-parse --short HEAD` (a `/healthz` 200 alone is not enough; a stale process passes it). The 6-unit grid rendering is the secondary visual confirm. `webapp.bat` is the headless/dev alternative (no tray). The Streamlit spike is a separate manual launch on :8501.

**TLS renewal:** the Let's Encrypt leaf is valid ~90 days, so renewal is **automated, not calendar-driven** — both boot paths (`webapp.bat` and the tray's `WebappManager.start()`) run `scripts/gen_tailscale_cert.py --check`, which re-issues only a `.ts.net` cert within 30 days of expiry (no-op otherwise). One-time prereq: enable HTTPS in the Tailscale admin console (DNS → HTTPS Certificates). See README.

See `README.md` for setup, layout, and usage.
