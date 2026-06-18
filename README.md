# home-automation

Proof-of-concept for reading and controlling Mitsubishi Electric units via
**MELCloud Home** (the newer Mitsubishi platform), ahead of building a solar
load-balancing automation on top of it.

> **Platform note.** These units migrated from classic MELCloud
> (`app.melcloud.com`) to **MELCloud Home**, which is a different API. The
> classic `pymelcloud` library cannot see them. This project uses
> [`aiomelcloudhome`](https://github.com/erwindouna/aiomelcloudhome) — a
> pure-async client that does the PKCE login over HTTP (no browser). Use
> your **MELCloud Home** credentials in `.env`.

## Layout

- **`src/`** — non-UI Python.
  - `melcloud_client.py` — async auth + fetch + control (the shared core).
  - `list_devices.py` — CLI that prints each unit's live state.
- **`app/`** — Streamlit control UI (`app.py`) over the same core.
- **`.env`** — credentials (gitignored; copy from `.env.example`).

## Setup

The virtual environment lives at `.venv`. Install dependencies:

```powershell
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt   # Windows
```

```bash
./.venv/bin/python -m pip install -r requirements.txt             # POSIX
```

## Configure credentials

```powershell
Copy-Item .env.example .env      # Windows
```

```bash
cp .env.example .env             # POSIX
```

Then edit `.env` and set `MELCLOUD_EMAIL` and `MELCLOUD_PASSWORD`.

## Run

CLI — print every device's live state:

```powershell
& .\.venv\Scripts\python.exe -m src.list_devices                  # Windows
```

```bash
./.venv/bin/python -m src.list_devices                            # POSIX
```

Streamlit — eyeball the same data in the browser. Use the launcher
(opens at http://localhost:8501):

```powershell
.\launch_app.bat                                                  # Windows
```

```bash
./launch_app.sh                                                   # POSIX
```

…or invoke Streamlit directly:

```powershell
& .\.venv\Scripts\python.exe -m streamlit run app/app.py          # Windows
```

```bash
./.venv/bin/python -m streamlit run app/app.py                    # POSIX
```
