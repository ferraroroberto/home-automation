# Layout

Where code goes in this repo, and the structure ratchet that holds it there. The shape is `webapp-tray`, declared in `.fleet.toml`; the canonical rules are project-scaffolding's `docs/repo-layout.md`. The exhaustive module-by-module inventory is [`docs/architecture.md`](architecture.md); this page is the map a change is placed by.

```
home-automation/
├── app/                       # UI and process layer — imports src, never the reverse
│   ├── webapp/                # the product: FastAPI + static PWA, plus the background tasks the app lifespan owns
│   │   ├── server.py          # app assembly, lifespan, auth wiring
│   │   ├── middleware.py      # token / password gate, request logging
│   │   ├── manager.py         # adopt-or-spawn / restart / stop for uvicorn (the tray's entry to the webapp)
│   │   ├── routers/           # one module per API area
│   │   └── static/            # the PWA: index.html, one JS module per view, styles/ (base.css + one file per view)
│   │       ├── styles/        # base.css plus one stylesheet per view
│   │       ├── vendor/        # third-party browser libraries, unmodified
│   │       └── _vendored/     # components copied byte-for-byte from project-scaffolding
│   ├── tray/                  # the Windows tray that owns the webapp lifecycle
│   └── .streamlit/            # Streamlit settings used by the spike
├── src/                       # UI-free domain logic: device clients, automations, stores, forecast and tariff maths
│   └── notify/                # vendored notification package (byte-for-byte from project-scaffolding)
├── tests/                     # backend suite (flat) and the browser suite
│   ├── api/                   # API smoke tests through FastAPI's TestClient
│   ├── ratchets/              # tests of the structure-ratchet gate wrapper
│   └── e2e/                   # Playwright suite and its fixtures
├── docs/                      # durable reference, one topic per file
├── scripts/                   # gate, ratchets, cert and auth helpers, ops tools
├── config/                    # committed *.sample.json templates; the live stores are gitignored
├── assets/                    # tray icon and Stream Deck art — not served by the PWA
├── custom_components/         # Home Assistant integration (declared exception)
└── spike/                     # standalone Streamlit POC (declared exception)
```

## Where new code goes

- A device client, a store, an automation rule or any logic with no UI: `src/`. It never imports `app`, and the layer check fails a `src` module that does.
- A route: `app/webapp/routers/`. A background task the lifespan owns: `app/webapp/`.
- A view: one JS module and one stylesheet in `app/webapp/static/` and `app/webapp/static/styles/`, not a new section in `index.html` or `base.css`.
- A tray behaviour: `app/tray/`.
- A script an operator runs by hand, or a gate stage: `scripts/`.
- A third-party browser library: `app/webapp/static/vendor/`. A scaffold component copied verbatim: its `[vendored]` entry's `dest`, never edited in place.

## Tests

- API tests go in `tests/api/`: they drive a route through `TestClient` with the cloud fetchers monkeypatched.
- Unit tests over `src/` domain logic go flat in `tests/`, which is past the directory limit and takes no new module. A new test that fits no existing file goes in a domain subpackage (`tests/ratchets/` holds the structure-ratchet gate test).
- Browser tests go in `tests/e2e/`. Its `conftest.py` holds the harness (server boot, browser lifecycle, collection hooks); a new API-stub fixture goes in `tests/e2e/fixtures/<area>.py` and is imported in `conftest.py`.

## Declared exceptions

The `webapp-tray` preset lists `app`, `src`, `tests`, `docs` as required and `scripts`, `data`, `brand`, `assets`, `config` as allowed. Two tracked root folders are outside it and are listed in `extra_roots`:

- **`custom_components/`** — Home Assistant loads a custom integration from a folder of this exact name under its config directory, so it cannot live under `app/` or `src/`. It is a thin API client and imports nothing from `src`.
- **`spike/`** — the independent Streamlit POC (`spike/streamlit_app.py`, launched by `launch_app.bat`). It shares only `src/melcloud_client.py` and is deliberately outside the product.

## The structure ratchet

`scripts/structure_ratchet.py` is vendored byte-for-byte from project-scaffolding (`.fleet.toml` `[vendored]`, key `structure_ratchet`) and run by the pre-ship gate through `scripts/structure_ratchet_gate.py`. It fails on:

- a new file over 1,000 lines, a new function over 100 lines, or a directory taking a new module past 40;
- a baselined offender growing past its size in `.structure-ratchet-baseline.json`;
- a `src` module importing `app`;
- a root folder that neither the preset nor `extra_roots` lists, or a required one with no tracked file.

Existing offenders sit in the baseline at their size at adoption: they may shrink but never grow, and splitting one is its own piece of work. Shrink the baseline with `--update-baseline`; only `--update-baseline --allow-additions` raises or adds an entry, and it shows in the diff. Exit 3 means nothing was checked and fails the gate.
