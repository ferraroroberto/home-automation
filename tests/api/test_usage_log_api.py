"""The vendored usage log is mounted and writes only where it is told (#906).

`src/usage_log.py` is vendored verbatim and tested in project-scaffolding; this
covers the app-side wiring only: the router answers on the app, a batch lands
in the file `USAGE_LOG_DB_PATH` names (set by `tests/conftest.py` before the
app is imported, so never the real runtime file), and an id outside the
privacy pattern is dropped rather than stored.
"""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from tests.conftest import USAGE_TEST_DB


def _rows(session_id: str) -> list[tuple]:
    with sqlite3.connect(USAGE_TEST_DB) as conn:
        return conn.execute(
            "SELECT kind, view_id, control_id, device_class, os_class FROM events"
            " WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()


def test_batch_lands_in_the_redirected_db(client: TestClient) -> None:
    session_id = "wiring-test-0001"
    res = client.post("/api/usage/events", json={
        "session_id": session_id,
        "device_class": "phone",
        "os_class": "ios",
        "events": [
            {"kind": "session_start"},
            {"kind": "view", "id": "ac"},
            {"kind": "tap", "id": "ac.power", "view": "ac"},
            {"kind": "tap", "id": "Turn on the living room", "view": "ac"},
        ],
    })
    assert res.status_code == 200
    assert res.json()["accepted"] == 3
    assert _rows(session_id) == [
        ("session_start", None, None, "phone", "ios"),
        ("view", "ac", None, "phone", "ios"),
        ("tap", "ac", "ac.power", "phone", "ios"),
    ]


def test_summary_route_is_mounted(client: TestClient) -> None:
    assert client.get("/api/usage/summary").status_code == 200
