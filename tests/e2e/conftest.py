"""Fixtures for the home-automation Playwright e2e suite.

The webapp's ``/api/units`` talks to the live MELCloud Home cloud, which
is slow, credential-dependent, and would actuate real HVAC units. So the
suite **boots the real webapp** (to serve index.html + the static PWA)
but **stubs the API with Playwright route interception** — the frontend
renders and is driven against deterministic fixtures, never the cloud.

Server lifecycle (adopt-or-autoboot):

* If something already answers ``/healthz`` on :8447, the suite adopts
  it (your dev `webapp.bat`).
* Otherwise it autoboots a disposable webapp on a free port (HTTPS when
  ``webapp/certificates/cert.pem`` exists, else HTTP). **Boot failure is
  a hard failure, never a skip** — a suite that skips when the app isn't
  up reports green on a build it never tested.

Dual projection: when ``--browser`` isn't passed the suite runs in two
projections — **Chromium desktop** and **WebKit projected onto an iPhone
14** (the iOS Mobile Safari engine family), so phone regressions surface
on Windows. A test marked ``desktop_only`` opts out of the WebKit run.
"""

from __future__ import annotations

import copy
import logging
import os
import signal
import socket
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable, Dict, IO, Iterator, List, Optional

import pytest
from playwright.sync_api import BrowserContext, Page, Route

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CERT = _REPO_ROOT / "webapp" / "certificates" / "cert.pem"
_KEY = _REPO_ROOT / "webapp" / "certificates" / "key.pem"
_ADOPT_PORT = 8447
_IPHONE_DEVICE = "iPhone 14"
_DEFAULT_TIMEOUT_MS = int(os.environ.get("E2E_DEFAULT_TIMEOUT_MS", "15000"))

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


# --------------------------------------------------------- sample fixtures
def _sample_units() -> List[Dict]:
    """Three deterministic fake units. Names are obvious fixtures, never
    the user's real rooms (the repo is public)."""
    modes = ["Heat", "Cool", "Automatic", "Dry"]
    fans = ["Auto", "One", "Two", "Three", "Four", "Five"]
    vert = ["Auto", "Swing", "One", "Two", "Three", "Four", "Five"]
    horiz = ["Auto", "Swing", "Left", "LeftCentre", "Centre", "RightCentre", "Right"]
    ranges = {"Heat": [10, 31], "Cool": [16, 31], "Automatic": [16, 31], "Dry": [16, 31]}
    return [
        {
            "unit_id": "unit-1", "name": "Office", "building": "Test",
            "power": True, "operation_mode": "Cool",
            "room_temperature": 22.5, "set_temperature": 24.0, "fan_speed": "Auto",
            "operation_modes": modes, "fan_speeds": fans,
            "temp_step": 0.5, "temp_ranges": ranges,
            "vane_vertical": "Auto", "vane_horizontal": "Swing",
            "vane_vertical_options": vert, "vane_horizontal_options": horiz,
            "has_vane_vertical": True, "has_vane_horizontal": True,
        },
        {
            "unit_id": "unit-2", "name": "Studio", "building": "Test",
            "power": False, "operation_mode": "Heat",
            "room_temperature": 19.0, "set_temperature": 21.0, "fan_speed": "Three",
            "operation_modes": modes, "fan_speeds": fans,
            "temp_step": 0.5, "temp_ranges": ranges,
            "vane_vertical": "Three", "vane_horizontal": None,
            "vane_vertical_options": vert, "vane_horizontal_options": [],
            "has_vane_vertical": True, "has_vane_horizontal": False,
        },
        {
            "unit_id": "unit-3", "name": "Loft", "building": "Test",
            "power": True, "operation_mode": "Automatic",
            "room_temperature": 20.0, "set_temperature": 22.0, "fan_speed": "Auto",
            "operation_modes": modes, "fan_speeds": fans,
            "temp_step": 0.5, "temp_ranges": ranges,
            "vane_vertical": None, "vane_horizontal": None,
            "vane_vertical_options": [], "vane_horizontal_options": [],
            "has_vane_vertical": False, "has_vane_horizontal": False,
        },
    ]


@pytest.fixture
def sample_units() -> List[Dict]:
    return copy.deepcopy(_sample_units())


# --------------------------------------------------------- server lifecycle
def _healthz_ok(base: str, timeout: float = 2.0) -> bool:
    try:
        req = urllib.request.Request(f"{base}/healthz")
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            return resp.status == 200
    except Exception:
        return False


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_healthz(base: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _healthz_ok(base):
            return True
        time.sleep(0.4)
    return False


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "desktop_only: skip on the WebKit/iPhone projection")
    selected: List[str] = config.option.browser
    if not selected:
        selected.extend(["chromium", "webkit"])


@pytest.fixture(scope="session")
def base_url() -> Iterator[str]:
    # Adopt a webapp already listening on :8447 (a dev webapp.bat).
    adopt = f"https://127.0.0.1:{_ADOPT_PORT}"
    if _healthz_ok(adopt):
        logger.info("✅ adopting live webapp at %s", adopt)
        yield adopt
        return
    adopt_http = f"http://127.0.0.1:{_ADOPT_PORT}"
    if _healthz_ok(adopt_http):
        logger.info("✅ adopting live webapp at %s", adopt_http)
        yield adopt_http
        return

    # Otherwise autoboot a disposable instance on a free port.
    port = _free_tcp_port()
    https = _CERT.exists() and _KEY.exists()
    scheme = "https" if https else "http"
    cmd = [
        sys.executable, "-m", "uvicorn", "app.webapp.server:app",
        "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
    ]
    if https:
        cmd += ["--ssl-keyfile", str(_KEY), "--ssl-certfile", str(_CERT)]

    logs_dir = _REPO_ROOT / "webapp"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_handle: IO[str] = (logs_dir / "e2e-autoboot.log").open(
        "w", encoding="utf-8", errors="replace"
    )
    kwargs: dict = dict(
        cwd=str(_REPO_ROOT), stdout=log_handle, stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
    )
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    proc = subprocess.Popen(cmd, **kwargs)

    base = f"{scheme}://127.0.0.1:{port}"
    try:
        if not _wait_healthz(base, timeout=15):
            raise pytest.fail.Exception(
                f"autoboot: webapp did not answer /healthz at {base} within 15s "
                "— see webapp/e2e-autoboot.log"
            )
        logger.info("✅ autoboot: webapp ready at %s", base)
        yield base
    finally:
        if proc.poll() is None:
            try:
                if sys.platform == "win32":
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        try:
            log_handle.close()
        except Exception:
            pass


# --------------------------------------------------------------- browser
@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict, browser_name: str, playwright) -> dict:
    # Self-signed cert — the SPA won't load otherwise.
    args = {**browser_context_args, "ignore_https_errors": True}
    if browser_name == "webkit":
        args = {**args, **playwright.devices[_IPHONE_DEVICE]}
    return args


@pytest.fixture(autouse=True)
def _skip_desktop_only_on_webkit(request: pytest.FixtureRequest, browser_name: str) -> None:
    if browser_name == "webkit" and request.node.get_closest_marker("desktop_only"):
        pytest.skip("desktop_only — not run on the WebKit/iPhone projection")


@pytest.fixture(autouse=True)
def _bound_default_timeouts(context: BrowserContext) -> None:
    context.set_default_timeout(_DEFAULT_TIMEOUT_MS)
    context.set_default_navigation_timeout(_DEFAULT_TIMEOUT_MS)


# --------------------------------------------------------- API stubbing
@pytest.fixture
def mock_api(page: Page) -> Callable[[List[Dict]], List[Dict]]:
    """Install route stubs for the units API on ``page``.

    ``GET /api/units`` returns the supplied list; ``POST /api/units/{id}``
    merges the JSON body into that unit and echoes it back (mirroring the
    server's read-back). Returns the live list so a test can assert the
    server-bound mutations. Call before navigating.
    """
    def _install(units: List[Dict]) -> List[Dict]:
        store = {u["unit_id"]: u for u in units}

        # Map the client's control field names onto the snapshot fields.
        field_map = {
            "set_temperature": "set_temperature",
            "power": "power",
            "operation_mode": "operation_mode",
            "fan_speed": "fan_speed",
            "vane_vertical_direction": "vane_vertical",
            "vane_horizontal_direction": "vane_horizontal",
        }

        def handle(route: Route) -> None:
            req = route.request
            if req.method == "GET":
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=_json({"units": list(store.values())}),
                )
                return
            # POST control → merge + echo back the updated snapshot.
            uid = req.url.rstrip("/").split("/")[-1]
            unit = store.get(uid)
            if unit is None:
                route.fulfill(status=404, content_type="application/json",
                              body=_json({"detail": "not found"}))
                return
            patch = req.post_data_json or {}
            for k, v in patch.items():
                if k in field_map:
                    unit[field_map[k]] = v
            route.fulfill(status=200, content_type="application/json", body=_json(unit))

        page.route("**/api/units", handle)
        page.route("**/api/units/*", handle)
        return list(store.values())

    return _install


def _json(obj) -> str:
    import json
    return json.dumps(obj)
