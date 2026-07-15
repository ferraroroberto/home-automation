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

import pytest


@pytest.fixture(autouse=True)
def _isolate_activity_stores(tmp_path, monkeypatch) -> None:
    """Point telemetry + JSONL activity logs at per-test temp paths."""
    import src.activity_log as activity_log
    import src.telemetry as tel

    monkeypatch.setattr(tel, "DEFAULT_DB_PATH", tmp_path / "telemetry.sqlite3")
    tel.init_db()
    monkeypatch.setattr(activity_log, "LOGS_DIR", tmp_path / "logs")
