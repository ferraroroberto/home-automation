"""Suite-wide fixtures.

The one job here: **never let a test write to the real activity stores.**
Producers mirror events into `src.telemetry` (#289), and the mirror is gated on
a process-global `default_db_ready` flag that the API-test layer flips on.
Without isolation, once that flag is set any later test's `append_activity`
(alarm / power / presence unit tests) mirrors fake events into the live
`webapp/telemetry.sqlite3` — the pollution bug fixed in #296. Pointing
`DEFAULT_DB_PATH` at a per-test temp DB for the whole tree closes it regardless
of flag state or test order. The same goes for the JSONL side of
`append_activity`: without redirecting `LOGS_DIR`, an endpoint that logs
activity (e.g. `/api/presence/locate`, #442) appends test fixtures into the
real `logs/*.jsonl` trail.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# The usage-log router (#906) binds its SQLite path when `app.webapp.server`
# builds the app at import, before any fixture runs, so the redirect has to be
# in the environment before the first test module imports the server. One
# throwaway file per run; the suite never reads the real usage.sqlite3.
USAGE_TEST_DB = Path(tempfile.mkdtemp(prefix="ha_usage_")) / "usage.sqlite3"
os.environ["USAGE_LOG_DB_PATH"] = str(USAGE_TEST_DB)

# The login password hash (src/password.py) is read on every login, so a run
# must never see the live config/app_password.json: point it at a file that
# does not exist. Assigned, not defaulted, so no outer value reaches the live
# file; servers the suite spawns inherit it.
os.environ["APP_PASSWORD_FILE"] = str(Path(tempfile.mkdtemp(prefix="home-automation-password-")) / "app_password.json")


# The restart record (src/self_restart.py) lives under this app's runtime-data
# directory; a run must never read or write the real one.
os.environ["HOME_AUTOMATION_DATA_DIR"] = tempfile.mkdtemp(prefix="home-automation-data-")


@pytest.fixture(autouse=True)
def _isolate_activity_stores(tmp_path, monkeypatch) -> None:
    """Point telemetry + JSONL activity logs at per-test temp paths."""
    import src.activity_log as activity_log
    import src.telemetry as tel

    monkeypatch.setattr(tel, "DEFAULT_DB_PATH", tmp_path / "telemetry.sqlite3")
    tel.init_db()
    monkeypatch.setattr(activity_log, "LOGS_DIR", tmp_path / "logs")


@pytest.fixture(autouse=True)
def _fresh_read_snapshots() -> None:
    """Start every test with empty read snapshots.

    They are module state, and loops outside the routers (the presence and
    power monitors) read through them too, so a value cached by one test's
    fake fetcher would otherwise answer the next test's read. A snapshot not
    yet registered starts empty when its router is imported.
    """
    from app.webapp import read_snapshot

    for snap in read_snapshot.registered():
        snap.reset()
