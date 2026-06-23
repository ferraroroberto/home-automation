"""Fixtures for the home-automation Playwright e2e suite.

The webapp's ``/api/units`` talks to the live MELCloud Home cloud, which
is slow, credential-dependent, and would actuate real HVAC units. So the
suite **boots the real webapp** (to serve index.html + the static PWA)
but **stubs the API with Playwright route interception** — the frontend
renders and is driven against deterministic fixtures, never the cloud.

Server lifecycle (adopt-or-autoboot):

* If something already answers ``/healthz`` on :8447, the suite adopts
  it (your dev `webapp.bat`) unless ``E2E_FORCE_AUTOBOOT=1`` is set.
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
from urllib.parse import unquote
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
    if os.environ.get("E2E_FORCE_AUTOBOOT") != "1":
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
        env={
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            # Never let the autobooted webapp hammer the real SMA devices — the
            # frontend is driven against stubbed energy fixtures, not the cloud.
            "ENERGY_SAMPLER_ENABLED": "0",
            # Same for the HVAC automation engine: never drive real units from a
            # test boot (the dormant-tick short-circuit makes it harmless with no
            # config, but keep it explicitly off like the sampler).
            "HVAC_AUTOMATION_ENABLED": "0",
            "PRESENCE_ICLOUD_REFRESH_ENABLED": "0",
            "PRESENCE_AUTOMATION_ENGINE_ENABLED": "0",
        },
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
        rule_store: Dict[str, Dict] = {}
        schedule_store: Dict[str, List[Dict]] = {}

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
            parts = req.url.split("/api/units", 1)[1].strip("/").split("/")
            if parts == [""] or parts == []:
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=_json({"units": list(store.values())}),
                )
                return

            uid = parts[0]
            if uid not in store:
                route.fulfill(status=404, content_type="application/json",
                              body=_json({"detail": "not found"}))
                return

            if len(parts) > 1 and parts[1] == "rule":
                if req.method == "GET":
                    route.fulfill(status=200, content_type="application/json",
                                  body=_json(rule_store.get(uid, {"enabled": False, "cool_target": None, "heat_target": None})))
                    return
                rule_store[uid] = req.post_data_json or {}
                route.fulfill(status=200, content_type="application/json", body=_json(rule_store[uid]))
                return

            if len(parts) > 1 and parts[1] == "schedule":
                if req.method == "GET":
                    entries = schedule_store.get(uid, [])
                    route.fulfill(status=200, content_type="application/json",
                                  body=_json({"enabled": any(e.get("enabled") for e in entries), "count": sum(1 for e in entries if e.get("enabled")), "next_time": None, "time": None, "entries": entries}))
                    return
                body = req.post_data_json or {}
                entries = body.get("entries", []) if isinstance(body, dict) else []
                schedule_store[uid] = entries
                enabled = [e for e in entries if e.get("enabled")]
                route.fulfill(status=200, content_type="application/json",
                              body=_json({"enabled": bool(enabled), "count": len(enabled), "next_time": enabled[0].get("time") if enabled else None, "time": enabled[0].get("time") if enabled else None, "entries": entries}))
                return

            # POST control → merge + echo back the updated snapshot.
            patch = req.post_data_json or {}
            for k, v in patch.items():
                if k in field_map:
                    store[uid][field_map[k]] = v
            route.fulfill(status=200, content_type="application/json", body=_json(store[uid]))

        page.route("**/api/units", handle)
        page.route("**/api/units/**", handle)
        return list(store.values())

    return _install


@pytest.fixture
def mock_energy(page: Page) -> Callable[..., None]:
    """Stub the four energy endpoints with deterministic fixtures.

    Covers the live snapshot (``/api/energy``), today's totals
    (``/api/energy/today``), the live-chart history (``/api/energy/history``),
    the history buckets (``/api/energy/aggregate``), and the tiered cost & savings
    breakdown (``/api/energy/cost``). Call before navigating. Defaults describe a
    sunny exporting moment so the flow row, charts, and cost table have content.
    """
    def _install(
        snapshot: Optional[Dict] = None,
        samples: Optional[List[Dict]] = None,
        buckets: Optional[List[Dict]] = None,
        today: Optional[Dict] = None,
        cost: Optional[Dict] = None,
    ) -> None:
        snap = snapshot or {
            "grid_import_w": 0.0, "grid_export_w": 1200.0,
            "pv_power_w": 2500.0, "house_consumption_w": 1300.0,
            "pv_surplus_w": 1200.0, "grid_import_kwh": None, "grid_export_kwh": None,
            "meter_reachable": True, "inverter_reachable": True, "meter_serial": None,
        }
        hist = samples if samples is not None else [
            {"ts": 1700000000, "pv_power_w": 2400.0, "house_consumption_w": 1200.0,
             "grid_import_w": 0.0, "grid_export_w": 1200.0, "pv_surplus_w": 1200.0,
             "inverter_reachable": True, "meter_reachable": True},
            {"ts": 1700000060, "pv_power_w": 2500.0, "house_consumption_w": 1300.0,
             "grid_import_w": 0.0, "grid_export_w": 1200.0, "pv_surplus_w": 1200.0,
             "inverter_reachable": True, "meter_reachable": True},
        ]
        aggs = buckets if buckets is not None else [
            {"key": "2026-06-19T10", "label": "10:00", "pv_wh": 1800.0,
             "house_wh": 1100.0, "import_wh": 0.0, "export_wh": 700.0,
             "pv_n": 60, "pv_missing": False},
            {"key": "2026-06-19T11", "label": "11:00", "pv_wh": 2100.0,
             "house_wh": 1250.0, "import_wh": 50.0, "export_wh": 900.0,
             "pv_n": 60, "pv_missing": False},
        ]
        today_bucket = today if today is not None else {
            "key": "2026-06-19", "label": "Fri 19", "pv_wh": 9000.0,
            "house_wh": 6000.0, "import_wh": 500.0, "export_wh": 3500.0,
            "pv_n": 300, "pv_missing": False,
        }
        page.route("**/api/energy", lambda r: r.fulfill(
            status=200, content_type="application/json", body=_json(snap)))
        page.route("**/api/energy/today", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=_json({"bucket": today_bucket})))
        page.route("**/api/energy/history*", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=_json({"minutes": 60, "samples": hist})))
        page.route("**/api/energy/aggregate*", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=_json({"range": "day", "buckets": aggs})))
        cost_body = cost if cost is not None else {
            "currency": "EUR", "tariff_name": "Test 2.0TD", "calendar": "2.0TD",
            "configured": True, "range": "day",
            "periods": [
                {"key": "P3", "label": "Off-peak", "hours": "0–8 · weekends",
                 "price_eur_kwh": 0.11, "rate_eur_kwh": 0.121,
                 "consumption_kwh": 2.0, "grid_kwh": 1.5, "solar_kwh": 0.5,
                 "generation_kwh": 0.5, "export_kwh": 0.0, "grid_cost": 0.18, "savings": 0.06},
                {"key": "P2", "label": "Standard", "hours": "8–10 · 14–18 · 22–24",
                 "price_eur_kwh": 0.13, "rate_eur_kwh": 0.143,
                 "consumption_kwh": 1.0, "grid_kwh": 0.4, "solar_kwh": 0.6,
                 "generation_kwh": 0.8, "export_kwh": 0.1, "grid_cost": 0.06, "savings": 0.09},
                {"key": "P1", "label": "Peak", "hours": "10–14 · 18–22",
                 "price_eur_kwh": 0.2, "rate_eur_kwh": 0.22,
                 "consumption_kwh": 1.5, "grid_kwh": 0.5, "solar_kwh": 1.0,
                 "generation_kwh": 1.2, "export_kwh": 0.2, "grid_cost": 0.11, "savings": 0.22},
            ],
            "totals": {"consumption_kwh": 4.5, "grid_kwh": 2.4, "solar_kwh": 2.1,
                       "generation_kwh": 2.5, "export_kwh": 0.3, "grid_cost": 0.35, "savings": 0.37},
            "summary": {"fixed_cost": 0.58, "export_credit": 0.0,
                        "cost_without_solar": 0.72, "estimated_bill": 0.93, "days": 1.0},
        }
        page.route("**/api/energy/cost*", lambda r: r.fulfill(
            status=200, content_type="application/json", body=_json(cost_body)))

    return _install


@pytest.fixture
def mock_security(page: Page) -> Callable[..., None]:
    """Stub the RISCO Security API with a small detector fixture."""
    def _install(snapshot: Optional[Dict] = None) -> None:
        state = snapshot or {
            "reachable": True,
            "label": "Disarmed",
            "mode": "disarmed",
            "supported_actions": ["partial", "perimeter", "arm"],
            "battery_low": False,
            "ac_lost": False,
            "assumed_control_panel_state": False,
            "zones": [
                {
                    "id": 1,
                    "name": "Front Door",
                    "type": 1,
                    "status": "closed",
                    "active": False,
                    "bypass": False,
                    "triggered": False,
                    "trouble": False,
                    "display_name": None,
                    "hidden": False,
                },
            ],
        }

        def handle(route: Route) -> None:
            url = route.request.url
            if "/api/security/events" in url:
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=_json({"events": []}),
                )
                return
            route.fulfill(status=200, content_type="application/json", body=_json(state))

        page.route("**/api/security**", handle)

    return _install


@pytest.fixture
def mock_presence(page: Page) -> Callable[..., None]:
    """Stub the read-only iCloud presence API with deterministic entities."""
    def _install(snapshot: Optional[Dict] = None) -> None:
        body = snapshot or {
            "available": True,
            "total_count": 3,
            "located_count": 2,
            "home_count": 1,
            "away_count": 1,
            "unknown_count": 1,
            "all_away": False,
            "home_radius_m": 200,
            "entities": [
                {
                    "entity_id": "home-phone",
                    "name": "Home Phone",
                    "model": "iPhone",
                    "device_class": "iPhone",
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "horizontal_accuracy_m": 8.0,
                    "last_seen": "2026-06-22T10:00:00+00:00",
                    "battery_level_pct": 80,
                    "battery_status": "Charging",
                    "distance_from_home_m": 50.0,
                    "at_home": True,
                    "display_name": None,
                    "hidden": False,
                    "source": "icloud",
                    "stale": False,
                },
                {
                    "entity_id": "away-phone",
                    "name": "Away Phone",
                    "model": "iPhone",
                    "device_class": "iPhone",
                    "latitude": 0.1,
                    "longitude": 0.0,
                    "horizontal_accuracy_m": 12.0,
                    "last_seen": "2026-06-22T09:45:00+00:00",
                    "battery_level_pct": 60,
                    "battery_status": "NotCharging",
                    "distance_from_home_m": 1100.0,
                    "at_home": False,
                    "display_name": None,
                    "hidden": False,
                    "source": "icloud",
                    "stale": False,
                },
                {
                    "entity_id": "tag",
                    "name": "Keys",
                    "model": "AirTag",
                    "device_class": "Accessory",
                    "latitude": None,
                    "longitude": None,
                    "horizontal_accuracy_m": None,
                    "last_seen": None,
                    "battery_level_pct": None,
                    "battery_status": None,
                    "distance_from_home_m": None,
                    "at_home": None,
                    "display_name": None,
                    "hidden": False,
                    "source": "icloud",
                    "stale": False,
                },
            ],
            "diagnostics": {"available": True, "reason": "ok", "detail": "", "refreshed_at": "2026-06-22T10:00:00+00:00"},
            "automation": {"enabled": False, "arm_away_after_s": 900, "stale_after_s": 3600, "disarm_on_arrival": True},
        }
        page.route("**/api/presence", lambda r: r.fulfill(
            status=200, content_type="application/json", body=_json(body)))
        page.route("**/api/location", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=_json({"lat": 0.0, "lon": 0.0, "label": "Home"})))
        page.route("**/api/location/reverse*", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=_json({"available": True, "label": "Fixture Place"})))

    return _install


@pytest.fixture
def sample_plugs() -> List[Dict]:
    """Four deterministic Tuya device cards covering each render branch:
    a metered plug (watts), a plain switch, a cover, and an offline device.
    All four are registered (has_valid_ip=True) so the default filter keeps
    them visible. Names are obvious fixtures, never the user's real devices
    (public repo)."""
    return [
        {
            "device_id": "plug-1", "name": "Test Heater", "category": "cz",
            "has_switch": True, "has_cover": False, "metered": True,
            "has_valid_ip": True, "reachable": True, "switch_on": True,
            "power_w": 1450.0, "current_ma": 6300.0, "voltage_v": 230.0,
            "energy_kwh": 12.5, "error": None,
        },
        {
            "device_id": "plug-2", "name": "Test Lamp", "category": "kg",
            "has_switch": True, "has_cover": False, "metered": False,
            "has_valid_ip": True, "reachable": True, "switch_on": False,
            "power_w": None, "current_ma": None, "voltage_v": None,
            "energy_kwh": None, "error": None,
        },
        {
            "device_id": "cover-1", "name": "Test Blind", "category": "cl",
            "has_switch": False, "has_cover": True, "metered": False,
            "has_valid_ip": True, "reachable": True, "switch_on": None,
            "power_w": None, "current_ma": None, "voltage_v": None,
            "energy_kwh": None, "error": None,
        },
        {
            "device_id": "plug-3", "name": "Test Offline", "category": "cz",
            "has_switch": True, "has_cover": False, "metered": True,
            "has_valid_ip": True, "reachable": False, "switch_on": None,
            "power_w": None, "current_ma": None, "voltage_v": None,
            "energy_kwh": None,
            "error": "Offline — refresh devices.json if this persists.",
        },
    ]


@pytest.fixture
def sample_plugs_with_no_ip(sample_plugs: List[Dict]) -> List[Dict]:
    """sample_plugs plus one no-IP adapter (has_valid_ip=False).
    Used to verify the default-filter and show-all toggle behaviour."""
    import copy
    devices = copy.deepcopy(sample_plugs)
    devices.append({
        "device_id": "plug-noip", "name": "Test NoIP", "category": "cz",
        "has_switch": True, "has_cover": False, "metered": False,
        "has_valid_ip": False, "reachable": False, "switch_on": None,
        "power_w": None, "current_ma": None, "voltage_v": None,
        "energy_kwh": None,
        "error": "No local IP — refresh devices.json on the home network.",
    })
    return devices


@pytest.fixture
def sample_lights() -> List[Dict]:
    """Two deterministic Elgato lights: one reachable and one offline."""
    return [
        {
            "light_id": "192.0.2.10:9123",
            "host": "192.0.2.10",
            "port": 9123,
            "name": "Fixture Key Light",
            "product_name": "Elgato Key Light",
            "firmware": "1.0",
            "on": True,
            "brightness": 42,
            "temperature": 200,
            "temperature_k": 5000,
            "supports_temperature": True,
            "reachable": True,
            "error": None,
        },
        {
            "light_id": "192.0.2.11:9123",
            "host": "192.0.2.11",
            "port": 9123,
            "name": "Fixture Offline",
            "product_name": None,
            "firmware": None,
            "on": False,
            "brightness": 0,
            "temperature": 0,
            "temperature_k": 0,
            "supports_temperature": False,
            "reachable": False,
            "error": "192.0.2.11:9123 timed out",
        },
    ]


@pytest.fixture
def mock_lights(page: Page) -> Callable[[List[Dict]], List[Dict]]:
    """Stub the Elgato lights API on ``page``."""
    def _install(lights: List[Dict]) -> List[Dict]:
        store = {item["light_id"]: item for item in lights}

        def handle(route: Route) -> None:
            req = route.request
            if req.method == "GET":
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=_json({"lights": list(store.values())}),
                )
                return
            light_id = unquote(req.url.rstrip("/").split("/")[-1])
            light = store.get(light_id)
            if light is None:
                route.fulfill(
                    status=404,
                    content_type="application/json",
                    body=_json({"detail": "not found"}),
                )
                return
            body = req.post_data_json or {}
            if "on" in body:
                light["on"] = bool(body["on"])
            if "brightness" in body:
                light["brightness"] = int(body["brightness"])
            if "temperature_k" in body:
                light["temperature_k"] = int(body["temperature_k"])
                light["temperature"] = round(1_000_000 / light["temperature_k"])
            route.fulfill(status=200, content_type="application/json", body=_json(light))

        page.route("**/api/lights", handle)
        page.route("**/api/lights/**", handle)
        return list(store.values())

    return _install


@pytest.fixture
def mock_tuya(page: Page) -> Callable[[List[Dict]], List[Dict]]:
    """Stub the local Tuya API on ``page``.

    ``GET /api/tuya`` returns the supplied device cards; the switch POST flips
    ``switch_on`` and echoes the card back (mirroring the server's read-back);
    the cover POST acknowledges the action. Returns the live list so a test can
    assert server-bound mutations. Call before navigating.
    """
    def _install(devices: List[Dict]) -> List[Dict]:
        store = {d["device_id"]: d for d in devices}

        def handle(route: Route) -> None:
            req = route.request
            if req.method == "GET":
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=_json({"devices": list(store.values())}),
                )
                return
            # POST .../switch|.../cover or PUT .../display_name on /api/tuya/{id}/{verb}
            parts = req.url.rstrip("/").split("/")
            verb, did = parts[-1], parts[-2]
            device = store.get(did)
            if device is None:
                route.fulfill(status=404, content_type="application/json",
                              body=_json({"detail": "not found"}))
                return
            body = req.post_data_json or {}
            if verb == "display_name":  # PUT — set/clear the override, echo it back
                name = (body.get("display_name") or "").strip()
                device["display_name"] = name or None
                route.fulfill(status=200, content_type="application/json",
                              body=_json({"device_id": did, "display_name": name or None}))
                return
            if verb == "switch":
                device["switch_on"] = bool(body.get("on"))
                route.fulfill(status=200, content_type="application/json",
                              body=_json(device))
                return
            route.fulfill(status=200, content_type="application/json",
                          body=_json({"device_id": did, "reachable": True,
                                      "action": body.get("action"), "ok": True}))

        page.route("**/api/tuya", handle)
        page.route("**/api/tuya/**", handle)
        return list(store.values())

    return _install


def _json(obj) -> str:
    import json
    return json.dumps(obj)
