"""Fixtures for the Python-level API smoke layer (``tests/api``).

These tests drive the **real** ``app.webapp.server:app`` in-process with
FastAPI's ``TestClient`` — the route handlers actually run, unlike the
browser-stubbed ``tests/e2e`` suite. Loopback bypasses the bearer gate, so no
auth setup is needed.

**No network, ever.** Two precautions keep a test boot off the real cloud:

* The background energy sampler and HVAC automation are disabled via env
  (set *before* the app module is imported), mirroring the e2e autoboot.
* The ``client`` fixture does **not** enter the ``TestClient`` context manager,
  so the app's ``lifespan`` startup never runs — no sampler/automation task is
  ever created. Cloud-backed routes are monkeypatched per-test instead.
"""

from __future__ import annotations

import os

# Must be set before importing app.webapp.server (which builds the app at
# import time and whose lifespan would otherwise spawn the sampler/automation).
os.environ.setdefault("ENERGY_SAMPLER_ENABLED", "0")
os.environ.setdefault("HVAC_AUTOMATION_ENABLED", "0")
os.environ.setdefault("PRESENCE_ICLOUD_REFRESH_ENABLED", "0")
os.environ.setdefault("PRESENCE_AUTOMATION_ENGINE_ENABLED", "0")

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _isolate_network_history(tmp_path, monkeypatch) -> None:
    """Point the network-history SQLite store at a per-test temp DB.

    ``GET /api/network`` records the seen devices into this registry (Phase 4),
    so without redirection every test hitting that route would write to the real
    ``webapp/network_history.sqlite3``. A fresh DB per test also keeps the
    new-device / offline derivations deterministic.
    """
    import src.network_history as nh

    monkeypatch.setattr(nh, "DEFAULT_DB_PATH", tmp_path / "network_history.sqlite3")

    # Telemetry isolation lives in the top-level ``tests/conftest.py`` so it
    # covers the alarm/power/presence unit tests too (the #296 pollution fix).


@pytest.fixture(scope="session")
def client() -> TestClient:
    """A ``TestClient`` over the real app, with lifespan intentionally not run.

    Not used as a context manager on purpose: entering it would run the app's
    ``lifespan`` startup. With the sampler/automation disabled above that is a
    no-op today, but skipping lifespan keeps the test hermetic regardless.
    """
    from app.webapp.server import app

    # Present as a loopback caller so the bearer gate is bypassed exactly as it
    # is for local probes — the API routes are reachable tokenless, matching the
    # documented security model. (Default TestClient host is "testclient".)
    return TestClient(app, client=("127.0.0.1", 12345))
