# Project Instructions

Canonical instructions for AI coding agents working in this repository. Claude Code reads this file directly as project memory. Other agents (Cursor, Codex, etc.) reach it via the one-line `AGENTS.md` pointer.

> Universal dev-workflow directives (plan mode, asking, before/while editing, git, branch & PR pipeline, documentation discipline) live once in the machine config (`~/.claude/CLAUDE.md`) and are not restated here. This file owns only what is specific to this project's shape.

## Streamlit conventions
*Apply only if this project uses Streamlit.*

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
- CLI smoke: `& .\.venv\Scripts\python.exe -m src.list_devices`
- Streamlit boot check: `& .\.venv\Scripts\python.exe -m streamlit run app/app.py --server.headless true`

There is no unit-test suite yet — say so plainly rather than claiming "tests pass."

## This repository
Proof-of-concept for reading and controlling Mitsubishi Electric HVAC units, ahead of a **solar load-balancing automation** (the eventual goal: shift HVAC load to match PV generation — see the sister `pvgis` repo for the solar-output estimate side).

**Platform: MELCloud Home, not classic MELCloud.** These units migrated from classic MELCloud (`app.melcloud.com`, served by `pymelcloud`) to **MELCloud Home**, a different API. `pymelcloud` authenticates to the old account but lists zero devices. This project uses [`aiomelcloudhome`](https://github.com/erwindouna/aiomelcloudhome) — a pure-async client that does the PKCE login over HTTP (no browser). Use **MELCloud Home** credentials in `.env`.

**Layout:**
- `src/melcloud_client.py` — async auth + fetch + control (the shared, UI-free core). `fetch_devices()` walks buildings → air-to-air units; `set_device_state()` writes via `control_ata_unit`. Capabilities drive the selectable modes, fan speeds, and per-mode temperature bounds.
- `src/list_devices.py` — CLI that prints each unit's live state.
- `app/app.py` — Streamlit control UI over the same core (unit picker + power / mode / target-temp / fan controls).

**Credentials:** `MELCLOUD_EMAIL` / `MELCLOUD_PASSWORD` in `.env` (gitignored, never committed) — these are the MELCloud Home login. The repo is **public**, so never commit credentials or unit/room names.

**Restart recipe:** no tray / long-lived daemon. The app is plain `streamlit run app/app.py` (via `launch_app.bat`). It has no hot-reload across imported-module changes, so after editing `src/` **fully restart** the process (kill the `:8501` listener and relaunch `launch_app.bat`) rather than relying on Streamlit's in-process rerun. The signal that new code is live is the unit list rendering via `aiomelcloudhome` (6 units), not just the page loading.

See `README.md` for setup, layout, and usage.
