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
- Keep the suite small — target < 15 tests total. No Page Object Model. Don't gate commits on e2e.
- When you remove a feature, remove its e2e test in the same commit.

## Verification (before declaring a task done)
Windows / PowerShell:
- Syntax: `& .\.venv\Scripts\python.exe -m py_compile <file>`
- CLI smoke: `& .\.venv\Scripts\python.exe -m src.list_devices` (HVAC) · `& .\.venv\Scripts\python.exe -m src.list_energy` (SMA energy)
- Webapp boot check: `& .\.venv\Scripts\python.exe -m uvicorn app.webapp.server:app --host 127.0.0.1 --port 8447` then `curl -k https://127.0.0.1:8447/healthz`, `…/api/units` and `…/api/energy` (loopback bypasses the token).
- Streamlit spike boot check: `& .\.venv\Scripts\python.exe -m streamlit run spike/streamlit_app.py --server.headless true`

There is no unit-test suite yet — say so plainly rather than claiming "tests pass."

## This repository
Proof-of-concept for reading and controlling Mitsubishi Electric HVAC units, ahead of a **solar load-balancing automation** (the eventual goal: shift HVAC load to match PV generation — see the sister `pvgis` repo for the solar-output estimate side).

**Platform: MELCloud Home, not classic MELCloud.** These units migrated from classic MELCloud (`app.melcloud.com`, served by `pymelcloud`) to **MELCloud Home**, a different API. `pymelcloud` authenticates to the old account but lists zero devices. This project uses [`aiomelcloudhome`](https://github.com/erwindouna/aiomelcloudhome) — a pure-async client that does the PKCE login over HTTP (no browser). Use **MELCloud Home** credentials in `.env`.

**Layout:**
- `src/melcloud_client.py` — async auth + fetch + control (the shared, UI-free core). `fetch_devices()` walks buildings → air-to-air units; `set_device_state()` writes via `control_ata_unit`. Capabilities drive the selectable modes, fan speeds, per-mode temperature bounds, and the two vanes (vertical/horizontal).
- `src/list_devices.py` — CLI that prints each unit's live state.
- `src/sma_client.py` — async, UI-free read of the **SMA solar/energy** devices (issue #21): prefer Sunny Portal cloud energy-balance when `SMA_CLOUD_PLANT_ID` is set, otherwise local Sunny Home Manager / energy meter over Speedwire (no creds → grid import/export) + the PV inverter over either local ennexOS or Speedwire (creds from `.env` → PV production). `fetch_energy_state()` returns the flattened `EnergyState` (grid/PV/consumption/net); an asleep or unconfigured inverter is `inverter_reachable=False`, not an error. Wraps `pysma-plus`.
- `src/list_energy.py` — CLI that prints the live energy flow (mirrors `list_devices.py`).
- `src/webapp_config.py` — webapp host/port + auth secrets (`auth_token` / `auth_password`); real `config/webapp_config.json` gitignored, `…sample.json` committed.
- `app/webapp/` — **the product**: FastAPI (`server.py` + `middleware.py` + `routers/{units,energy,auth,misc}.py`) over the same core, serving a static PWA (`static/`). `GET /api/units` → `fetch_devices()`; `POST /api/units/{id}` → `set_device_state(...)`; `GET /api/energy` → `fetch_energy_state()`. Card grid with inline controls + a top energy-flow tile; per-unit detail modal for mode + vanes. `manager.py` (adopt-or-spawn / restart / stop for uvicorn, reading host/port from `webapp_config`) lives here too — at the **canonical fleet path** `app.webapp.manager` so the `projects.toml` `restart_cmd` is identical to every other tray-owned app.
- `app/tray/` — the **Windows tray** that owns the webapp lifecycle (`tray.bat` → `python -m app.tray`). `tray.py` (pystray icon + menu), `__main__.py` (entry); the tray imports `WebappManager` from `app.webapp.manager`. `single_instance.py` + `tray_lifecycle.ps1` are **vendored verbatim** from `project-scaffolding` — never edit per-app.
- `scripts/` — `gen_ssl_cert.py` (self-signed CA+leaf, Tailscale-aware SANs), `gen_token.py` / `set_password.py` (auth), `gen_icons.py` (PWA icons; Pillow dev-only).
- `spike/streamlit_app.py` — the **independent POC spike** (throwaway data/debug view; shares only `src/melcloud_client.py`), launched via `launch_app.bat` on :8501.

**Credentials & secrets:** `MELCLOUD_EMAIL` / `MELCLOUD_PASSWORD` in `.env` (the MELCloud Home login). The repo is **public** — never commit credentials, the bearer token / password (`config/webapp_config.json`), the TLS keys (`webapp/certificates/`), or unit/room names. All are gitignored.

**Security model:** the webapp binds `0.0.0.0:8447` and is reached over LAN / **Tailscale** behind a **self-signed-CA HTTPS** endpoint + an optional **bearer token** (loopback bypasses; remote needs `Authorization: Bearer` or `?token=`). Mirrors the `photo-ocr` / `app-launcher` stack. No cloudflared tunnel, no WebAuthn passkey (there's no terminal here).

**Restart recipe:** the webapp is owned by the **tray** (`tray.bat` → `python -m app.tray`, on :8447, HTTPS when `webapp/certificates/cert.pem` is present). No hot-reload across imported-module changes, so after editing `src/` or `app/`, **`tray.bat --restart`** — it kills the old tray subtree, orphan-proof-reclaims `:8447` (scoped to this repo's `.venv` by CommandLine), and starts a fresh tray. The signal that new code is live is the served build identity: `GET /api/version` returns `{git_sha, built_at}` and the PWA shows a `Build: <sha> · <ts>` footer — the `git_sha` must match `git rev-parse --short HEAD` (a `/healthz` 200 alone is not enough; a stale process passes it). The 6-unit grid rendering is the secondary visual confirm. `webapp.bat` is the headless/dev alternative (no tray). The Streamlit spike is a separate manual launch on :8501.

**TLS rotation:** the leaf cert expires ~396 days after generation — **regenerate before ~July 2027** (`scripts/gen_ssl_cert.py`, reuses the CA so no device re-trust). See README.

See `README.md` for setup, layout, and usage.
