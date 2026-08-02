"""Fixtures for the home-automation Playwright e2e suite.

The webapp's ``/api/units`` talks to the live MELCloud Home cloud, which
is slow, credential-dependent, and would actuate real HVAC units. So the
suite **boots the real webapp** (to serve index.html + the static PWA)
but **stubs the API with Playwright route interception** — the frontend
renders and is driven against deterministic fixtures, never the cloud.

Server lifecycle, three modes (issue #538 — the polarity used to be
inverted: a bare run silently adopted whatever answered ``/healthz`` on
:8447, which is the tray this repo's real security alarm, HVAC, and Tuya
plugs run through):

* **Default (bare ``pytest tests/e2e``).** Autoboots a disposable webapp
  on a free port (HTTPS when ``webapp/certificates/cert.pem`` exists,
  else HTTP). **Boot failure is a hard failure, never a skip** — a suite
  that skips when the app isn't up reports green on a build it never
  tested. If the tray's port (:8447) happens to be occupied, this mode
  never touches it either way — it just boots its own instance alongside.
* **Live (explicit opt-in).** ``E2E_LIVE=1`` means the caller has chosen
  to *act* on whatever's already listening on :8447 (this repo's tray).
  Guarded by the vendor-verbatim ``tests/e2e/_e2e_live_guard.py``
  (project-scaffolding issue #191/#194 — same module every fleet adopter
  copies byte-identical; see its own docstring for the fleet-wide policy).
  Without the flag, an occupied :8447 makes the guard refuse via
  ``pytest.exit`` naming ``E2E_LIVE`` — an accidental bare run must not
  silently load-test (or actuate!) the tray. This repo's deliberate choice
  on an opt-in hit is **read-only adoption, never a restart or kill**: the
  tray's port isn't just a daily-driver dashboard (voice-transcriber's
  reasoning for the same choice) — it fronts a real RISCO security alarm,
  real HVAC units, and real Tuya plugs, so anything beyond what the
  suite's own Playwright route-stubbing already isolates could actuate a
  real device. A restart-to-reclaim is never appropriate here either: the
  whole point of opting in is to reuse the *running* instance, not bounce
  it mid-suite.
* **Autoboot when the port is free.** Same disposable-instance path as
  the default mode, reached whenever :8447 isn't occupied (with or
  without ``E2E_LIVE`` set — there's nothing live to adopt).

Dual projection: when ``--browser`` isn't passed the suite runs in two
projections — **Chromium desktop** and **WebKit projected onto an iPhone
14** (the iOS Mobile Safari engine family), so phone regressions surface
on Windows. A test marked ``desktop_only`` opts out of the WebKit run.

``pytest_sessionfinish`` runs the vendor-verbatim leaked-browser-helper
sweep (``tests/e2e/_browser_sweep.py``, project-scaffolding #203/#204)
once the whole session — fixtures included — has torn down, so a run that
orphaned a WebKit helper reclaims it *while it is still killable*, instead
of leaving one pinning this checkout's directory (which is what makes a
later ``git worktree remove`` fail as "busy").
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timezone
from urllib.parse import unquote
from pathlib import Path
from typing import Callable, Dict, IO, Iterator, List, Optional

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, Route, sync_playwright

from app.webapp.event_loop import LOOP_FACTORY
from tests.e2e._browser_sweep import sweep_browser_helpers
from tests.e2e._e2e_live_guard import require_disposable_instance

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CERT = _REPO_ROOT / "webapp" / "certificates" / "cert.pem"
_KEY = _REPO_ROOT / "webapp" / "certificates" / "key.pem"


def _adopt_port() -> int:
    """The port *this checkout's* own webapp would serve on (usually 8447).

    Read from ``config/webapp_config.json`` rather than hardcoded, because
    "the live instance" is per checkout: a concurrent `git worktree` session
    (fleet-config's worktree_claim workflow, fleet-config#537) rewrites its
    own copy's ``port`` at setup time so it never resolves to the primary
    checkout's tray. With a hardcoded 8447 the guard below refused every
    worktree run — even though the run would have autobooted a disposable
    instance on a free port and never gone near the tray. On the primary
    checkout this resolves to 8447 exactly as before.

    ``load_webapp_config()`` already falls back to the default (8447) when
    the file is missing or unreadable, so nothing here needs to guess at
    that case. What's left uncaught — a config that parses but carries an
    invalid ``port`` (out of range, wrong type) — is left to raise: silently
    substituting 8447 there could wrongly declare a collision with the
    primary's tray from inside a worktree whose config is broken, the exact
    false positive this function exists to avoid (#593). A hard failure at
    collection time is the honest outcome when this checkout's own port
    can't be determined.
    """
    from src.webapp_config import load_webapp_config

    return int(load_webapp_config().port)


_ADOPT_PORT = _adopt_port()
# Explicit opt-in for adopting the LIVE tray on this checkout's port (issue
# #538) — the flag name passed to the vendored
# _e2e_live_guard.require_disposable_instance. Without it, an occupied port
# makes a bare `pytest tests/e2e` refuse rather than silently drive the tray
# fronting a real security alarm/HVAC/Tuya plugs.
_LIVE_ENV = "E2E_LIVE"
_IPHONE_DEVICE = "iPhone 14"
_DEFAULT_TIMEOUT_MS = int(os.environ.get("E2E_DEFAULT_TIMEOUT_MS", "15000"))
# Bound for the session-scoped browser/driver teardown (#440): a stale
# WebKitNetworkProcess zombie from a previously-killed run can wedge
# browser.close()/playwright.stop() forever, so pytest never reaches its
# final summary line even though every test already passed. Bounding the
# wait lets pytest proceed to exit regardless; the worst case is a leaked
# driver process, not a hung suite.
_TEARDOWN_TIMEOUT_S = float(os.environ.get("E2E_TEARDOWN_TIMEOUT_S", "15"))
# Throwaway telemetry DB for the autobooted webapp, so a control action in the
# e2e flow can't mirror events into the real webapp/telemetry.sqlite3 (#296).
_E2E_TELEMETRY_DB = Path(tempfile.gettempdir()) / "ha_e2e_telemetry.sqlite3"

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


_WMI_DATE_RE = re.compile(r"^/Date\((-?\d+)\)/$")


def _reap_orphaned_webkit_zombies() -> None:
    """Best-effort kill of orphaned ``WebKitNetworkProcess`` zombies before this
    session boots its own browser (#480). #440's bounded teardown accepted a
    leak as its tradeoff: a killed-mid-run WebKit driver can leave its
    network-process child behind with no reap path, so the leak grows
    unbounded across sessions/days.

    #480 set out to confirm accumulated zombies as the cause of an observed
    ~4min suite stretching to ~1hr, but that hypothesis did **not** hold up:
    a full suite run measured with 40 real accumulated zombies present still
    completed at the normal ~4min baseline. This sweep is kept anyway as
    defense-in-depth hygiene, not as a fix for that slowdown (root cause
    unconfirmed) — most of the 40 also turned out to be true Windows zombie
    processes (`tasklist`/WMI still list them, but `Get-Process`/`taskkill`
    can't see or touch them — already exited, kernel object pinned by a
    stale handle), so this can only actually reap a *recently* orphaned,
    still-live process, before it reaches that unkillable state.

    A "does the recorded parent PID still exist" check alone is unsafe on
    Windows: PIDs are reused, so a long-dead zombie's old parent PID can
    coincidentally match an unrelated process started later (observed in
    practice: a zombie parented by a WebKit driver process from days ago had
    its PID recycled by the current tray-managed webapp). Comparing
    creation timestamps closes that gap — a "parent" created *after* the
    child can't be its real parent, so treat that as orphaned too. This
    also keeps a live, in-progress e2e run's zombies untouched: their real
    parent driver process is alive and genuinely older than the child.
    """
    if sys.platform != "win32":
        return
    ps_script = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ParentProcessId,Name,CreationDate | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            [
                "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
                "-NoProfile", "-NonInteractive", "-Command", ps_script,
            ],
            capture_output=True, text=True, timeout=20,
        )
        procs = json.loads(result.stdout) if result.stdout.strip() else []
    except Exception:
        logger.warning("⚠️ zombie WebKitNetworkProcess sweep: process listing failed, skipping")
        return
    if isinstance(procs, dict):
        procs = [procs]

    def _parse(ts: Optional[str]) -> Optional[datetime]:
        # Get-CimInstance's ConvertTo-Json renders CreationDate as an ISO 8601
        # string when queried through a server-side WQL -Filter, but as the
        # legacy "/Date(<epoch-ms>)/" form when the full Win32_Process table
        # is piped through Select-Object first (observed empirically) — both
        # formats show up depending on how the process list is fetched.
        if not ts:
            return None
        wmi_match = _WMI_DATE_RE.match(ts)
        if wmi_match:
            return datetime.fromtimestamp(int(wmi_match.group(1)) / 1000, tz=timezone.utc)
        try:
            return datetime.fromisoformat(ts)
        except Exception:
            return None

    by_pid = {p["ProcessId"]: p for p in procs if p.get("ProcessId") is not None}
    zombies = [p for p in procs if p.get("Name") == "WebKitNetworkProcess.exe"]
    if not zombies:
        return

    killed = 0
    unreapable = 0
    for zombie in zombies:
        pid = zombie["ProcessId"]
        child_created = _parse(zombie.get("CreationDate"))
        parent = by_pid.get(zombie.get("ParentProcessId"))
        parent_created = _parse(parent.get("CreationDate")) if parent else None
        orphaned = parent is None or (
            child_created is not None
            and parent_created is not None
            and parent_created > child_created
        )
        if not orphaned:
            continue
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=10,
            )
            if result.returncode == 0:
                killed += 1
            else:
                unreapable += 1
        except Exception:
            unreapable += 1
    if killed:
        logger.info(
            "🧹 zombie sweep: reaped %d orphaned WebKitNetworkProcess process(es) (#480)",
            killed,
        )
    if unreapable:
        logger.info(
            "ℹ️ zombie sweep: %d orphaned WebKitNetworkProcess process(es) could not be "
            "reaped — already in an unkillable Windows zombie state (clears only when "
            "the stale handle-holder releases it, or on reboot; see #480)",
            unreapable,
        )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "desktop_only: skip on the WebKit/iPhone projection")
    selected: List[str] = config.option.browser
    if not selected:
        selected.extend(["chromium", "webkit"])
    _reap_orphaned_webkit_zombies()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Sweep browser helpers this run orphaned inside *this* checkout (#583).

    A session hook, not a fixture finalizer: it must run after *every*
    fixture — including the session-scoped ``playwright``/``browser``
    fixtures above — has already torn down, or the sweep would be looking at
    a browser that is still legitimately running. The scope path is the only
    call-site argument, so ``_browser_sweep.py`` stays byte-identical to
    project-scaffolding's copy.

    Advisory by design: it reports and never touches ``exitstatus``, because
    an already-exited handle-held zombie is unkillable and is not a test
    failure (see ``_browser_sweep``'s module docstring for why those exist,
    and why nothing is ever killed by image name alone — Chromium is
    deliberately out of the sweep set).
    """
    result = sweep_browser_helpers(_REPO_ROOT)
    print(f"\n{result.summary()}")
    for entry in result.killed:
        print(f"  reclaimed leaked helper: {entry}")


@pytest.fixture(scope="session")
def base_url() -> Iterator[str]:
    # Vendored guard (issue #538): refuses via pytest.exit if :8447 is
    # occupied and E2E_LIVE isn't set. This repo's caller-side choice on an
    # opt-in hit is read-only adoption of the live tray, never a restart or
    # kill — see the module docstring above for why.
    live_opt_in = require_disposable_instance(_ADOPT_PORT, _LIVE_ENV)
    if live_opt_in:
        adopt = f"https://127.0.0.1:{_ADOPT_PORT}"
        if _healthz_ok(adopt):
            logger.info("✅ %s=1 — adopting live webapp at %s", _LIVE_ENV, adopt)
            yield adopt
            return
        adopt_http = f"http://127.0.0.1:{_ADOPT_PORT}"
        if _healthz_ok(adopt_http):
            logger.info("✅ %s=1 — adopting live webapp at %s", _LIVE_ENV, adopt_http)
            yield adopt_http
            return

    # Autoboot a disposable instance on a free port — the default, and the
    # fallback if the opt-in was set but the port turned out not to actually
    # answer /healthz (a race between the guard's raw socket check and here).
    port = _free_tcp_port()
    https = _CERT.exists() and _KEY.exists()
    scheme = "https" if https else "http"
    cmd = [
        sys.executable, "-m", "uvicorn", "app.webapp.server:app",
        "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
        "--loop",
        LOOP_FACTORY,
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
            # Never let the autobooted webapp hammer the real FusionSolar cloud — the
            # frontend is driven against stubbed energy fixtures, not the cloud.
            "ENERGY_SAMPLER_ENABLED": "0",
            # Likewise the telemetry reading sampler — it would otherwise fetch
            # real HVAC (cloud) / plugs / UPS / lights every few minutes from a
            # test boot. The activity UI is driven against the API directly.
            "TELEMETRY_SAMPLER_ENABLED": "0",
            # And redirect the telemetry DB to a throwaway file so control
            # actions in the e2e flow (arm/disarm/plug toggles) can't mirror
            # events into the real webapp/telemetry.sqlite3 (#296).
            "TELEMETRY_DB_PATH": str(_E2E_TELEMETRY_DB),
            # Same for the HVAC automation engine: never drive real units from a
            # test boot (the dormant-tick short-circuit makes it harmless with no
            # config, but keep it explicitly off like the sampler).
            "HVAC_AUTOMATION_ENABLED": "0",
            "SECURITY_SCHEDULES_ENABLED": "0",
            "PRESENCE_ICLOUD_REFRESH_ENABLED": "0",
            "PRESENCE_AUTOMATION_ENGINE_ENABLED": "0",
            # Assist trace ingestion is a production background read. Browser
            # tests stub /api/ha and must never inspect the live HA instance.
            "HA_TRACE_ENABLED": "0",
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
def _driver_pid(pw: Optional[Playwright]) -> Optional[int]:
    """Best-effort reach into Playwright's private driver-process handle so a
    wedged teardown (#440) can be force-killed. ``pw.stop`` is the
    ``PlaywrightContextManager.__exit__`` bound method (see
    ``playwright/sync_api/_context_manager.py``); its ``__self__`` holds the
    connection → transport → asyncio subprocess chain down to the Node
    driver's PID. Reaches into ``_impl`` privates on purpose — there is no
    public accessor — so this degrades to ``None`` (no forced kill, just a
    log warning) rather than raising if a future Playwright version changes
    this shape.
    """
    if pw is None:
        return None
    try:
        ctx_mgr = pw.stop.__self__  # type: ignore[attr-defined]
        return ctx_mgr._connection._transport._proc.pid  # type: ignore[attr-defined]
    except Exception:
        return None


def _bounded_teardown(fn: Callable[[], None], label: str, driver_pid: Optional[int]) -> None:
    """Run a session-scope teardown call bound to a hard wall-clock limit (#440).

    A stale WebKitNetworkProcess zombie from a previously-killed run can wedge
    the Node driver's own exit-time cleanup, so ``browser.close()`` /
    ``playwright.stop()`` never return and pytest hangs after the last test,
    never reaching its summary line. Playwright's sync API is greenlet-based
    and thread-affine (``fn`` must run on THIS thread — see the
    ``greenlet.error: Cannot switch to a different thread`` a naive
    background-thread call raises), so a watchdog thread can't just call
    ``fn`` for us. What it *can* safely do off-thread is force-kill the
    driver's OS process; that breaks the pipe ``fn`` is blocked reading from,
    which Playwright's own error path (`Connection.cleanup()`) turns into an
    exception rather than a silent hang, letting pytest proceed.
    """
    done = threading.Event()
    timed_out = threading.Event()

    def _watchdog() -> None:
        if done.wait(timeout=_TEARDOWN_TIMEOUT_S):
            return
        timed_out.set()
        logger.warning(
            "⚠️ %s did not complete within %.0fs — force-killing the driver "
            "process (known WebKitNetworkProcess zombie hang, #440)",
            label, _TEARDOWN_TIMEOUT_S,
        )
        if driver_pid is None:
            return
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(driver_pid)],
                    capture_output=True, timeout=10,
                )
            else:
                os.kill(driver_pid, signal.SIGKILL)
        except Exception:
            logger.warning("⚠️ could not force-kill driver pid %s", driver_pid)

    threading.Thread(target=_watchdog, name="e2e-teardown-watchdog", daemon=True).start()
    try:
        fn()
    except Exception:
        if not timed_out.is_set():
            raise
        logger.warning("⚠️ %s raised after forced kill (expected) — ignoring", label)
    finally:
        done.set()


@pytest.fixture(scope="session")
def playwright(browser_name: str) -> Iterator[Playwright]:
    """One Playwright driver **per browser projection**, not one per session (#584).

    The `browser_name` dependency is the entire fix, and it is load-bearing
    rather than decorative. pytest-playwright parametrizes `browser_name` at
    session scope, so taking it as an argument makes pytest build (and finalize)
    one instance of this fixture per projection instead of a single shared one.

    Why that matters: `_bounded_teardown` unwedges a hung teardown by
    force-killing the driver's OS process — the only thing a watchdog thread may
    safely do, since Playwright's sync API is greenlet-bound to the calling
    thread. With one driver shared across projections, a wedged Chromium
    `browser.close()` killed the driver WebKit was about to use, and every
    later launch failed with "Connection closed while reading from the driver":
    one error per remaining test, no summary line, pytest spinning at ~90% CPU
    until killed. The kill was correct; its blast radius was not.

    Per-projection drivers keep #440's guarantee (a wedged teardown is still
    bounded and still force-killed) while confining the damage to the projection
    that actually wedged. `tests/e2e/test_driver_isolation.py` pins this.
    """
    pw = sync_playwright().start()
    yield pw
    _bounded_teardown(pw.stop, f"playwright.stop() [{browser_name}]", _driver_pid(pw))


@pytest.fixture(scope="session")
def browser(launch_browser: Callable[..., Browser], playwright: Playwright) -> Iterator[Browser]:
    browser_instance = launch_browser()
    yield browser_instance
    _bounded_teardown(browser_instance.close, "browser.close()", _driver_pid(playwright))


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
@pytest.fixture(autouse=True)
def _stub_home_assistant_api(page: Page) -> None:
    """Never let a browser regression test inspect the household's live HA."""

    page.route(
        "**/api/ha",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"satellites":[],"interactions":[],"voice_transcriber":true}',
        ),
    )


@pytest.fixture(autouse=True)
def _stub_circuits_api(page: Page) -> None:
    """Never let a browser regression test run a live mDNS sweep (issue #25).

    Unstubbed, ``GET /api/circuits`` really browses the LAN, so the suite's
    results would depend on whether a CT-clamp meter happens to be powered on —
    and every discovered channel adds a ``.device-row`` to the IoT tab, which
    silently broke the plug row counts. Same reasoning as the HA stub above.
    Tests that want meters install their own route over this one.
    """

    page.route(
        "**/api/circuits",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"meters":[],"discovery_ok":true,"error":null}',
        ),
    )


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
    """Stub the energy endpoints with deterministic fixtures.

    Covers the live snapshot (``/api/energy``), today's totals
    (``/api/energy/today``), the live-chart history (``/api/energy/history``),
    the history buckets (``/api/energy/aggregate``), the tiered cost & savings
    breakdown (``/api/energy/cost``), the solar forecast
    (``/api/energy/forecast``), the PV-system config it is computed from
    (``/api/energy/pv-system``, issue #561), the read-only sun-position
    diagnostic (``/api/energy/sun-overlay``, issue #590) and the fleet
    solar-boost sequencing knobs (``/api/hvac/boost-coordinator``, issue #562 —
    an HVAC path whose card lives on this tab). Call before navigating. Defaults
    describe a sunny exporting moment so the flow row, charts, and cost table
    have content.

    The PV-system route is **stateful**: a PUT updates the in-memory rows, and
    the forecast's ``system`` block is derived from them, so a test can assert
    that saving the editor is reflected back in the forecast card. Stubbing
    both also keeps any Energy-tab test off the network — an unstubbed forecast
    would have the disposable instance call Open-Meteo for real.
    """
    def _install(
        snapshot: Optional[Dict] = None,
        samples: Optional[List[Dict]] = None,
        buckets: Optional[List[Dict]] = None,
        today: Optional[Dict] = None,
        cost: Optional[Dict] = None,
        pv_arrays: Optional[List[Dict]] = None,
        boost_coord: Optional[Dict] = None,
        forecast: Optional[Dict] = None,
        sun_overlay: Optional[Dict] = None,
        today_gap_hours: float = 0.0,
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
        # `gap_hours` rides beside the bucket, not inside it (#579), and drives
        # a different card than the forecast overlay does — so it is its own
        # knob rather than a field of `today`.
        page.route("**/api/energy/today", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=_json({"bucket": today_bucket, "gap_hours": today_gap_hours})))
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

        pv = {
            "arrays": list(pv_arrays) if pv_arrays is not None else [
                {"kwp": 7.9, "tilt_deg": 15.0, "azimuth_deg": 0.0},
            ],
            "performance_ratio": 0.8,
        }

        def _pv_body() -> Dict:
            return {
                "configured": bool(pv["arrays"]),
                "arrays": pv["arrays"],
                "performance_ratio": pv["performance_ratio"],
                "total_kwp": round(sum(a["kwp"] for a in pv["arrays"]), 3),
            }

        def handle_pv_system(route: Route) -> None:
            if route.request.method.upper() == "PUT":
                sent = route.request.post_data_json or {}
                pv["arrays"] = sent.get("arrays") or []
                if sent.get("performance_ratio") is not None:
                    pv["performance_ratio"] = sent["performance_ratio"]
            route.fulfill(
                status=200, content_type="application/json", body=_json(_pv_body()))

        page.route("**/api/energy/pv-system", handle_pv_system)

        # The fleet solar-boost sequencing knobs (issue #562). Path is
        # /api/hvac/... because it is HVAC config, but it is stubbed here because
        # its card lives on the Energy tab — so every Energy-tab test that
        # already calls this fixture keeps its network stubbed.
        boost = {
            "settle_interval_s": 300, "admission_margin_w": 0.0,
            "hard_deficit_w": 1000.0, "ordering_policy": "stable",
        }
        if boost_coord:
            boost.update(boost_coord)

        def _boost_body() -> Dict:
            return dict(
                boost, min_settle_interval_s=300, ordering_policies=["stable"]
            )

        def handle_boost_coord(route: Route) -> None:
            if route.request.method.upper() == "PUT":
                sent = route.request.post_data_json or {}
                # Mirrors the server: an out-of-range value is a 400 naming the
                # field, never a silent clamp.
                settle = sent.get("settle_interval_s")
                if settle is not None and int(settle) < 300:
                    route.fulfill(
                        status=400, content_type="application/json",
                        body=_json({"detail": "settle_interval_s must be at least 300 seconds"}))
                    return
                for key, value in sent.items():
                    if value is not None:
                        boost[key] = value
            route.fulfill(
                status=200, content_type="application/json", body=_json(_boost_body()))

        page.route("**/api/hvac/boost-coordinator", handle_boost_coord)

        def handle_forecast(route: Route) -> None:
            body = {
                "available": True,
                "day": "today",
                "expected": [{"hour": h, "wh": 0.0 if h < 8 else 900.0} for h in range(24)],
                "expected_total_kwh": 12.3,
                "actual": None,
                "actual_gap_hours": None,
                "system": {
                    "arrays": pv["arrays"],
                    "total_kwp": round(sum(a["kwp"] for a in pv["arrays"]), 3),
                    "performance_ratio": pv["performance_ratio"],
                },
            }
            # `system` stays derived from the (stateful) PV rows even when a
            # test overrides the curve, so the save-reflects-in-the-card tests
            # keep working alongside an overridden actual overlay.
            if forecast:
                body.update(forecast)
            route.fulfill(status=200, content_type="application/json", body=_json(body))

        page.route("**/api/energy/forecast*", handle_forecast)

        # Read-only sun-position diagnostic (#590). Stubbed here for the same
        # reason the forecast is: its card lives on this tab and an unstubbed
        # call would have the disposable instance reach Open-Meteo for real.
        # Default is an empty-but-available overlay, so a test that never opens
        # the (collapsed) card is unaffected.
        def handle_sun_overlay(route: Route) -> None:
            body = {
                "available": True,
                "date": "2026-07-30",
                "modelled_pr": pv["performance_ratio"],
                "points": [],
                "excluded": [],
                "excluded_coverage": 0,
                "excluded_no_data": 0,
                "system": {
                    "arrays": pv["arrays"],
                    "total_kwp": round(sum(a["kwp"] for a in pv["arrays"]), 3),
                    "performance_ratio": pv["performance_ratio"],
                },
            }
            if sun_overlay:
                body.update(sun_overlay)
            route.fulfill(status=200, content_type="application/json", body=_json(body))

        page.route("**/api/energy/sun-overlay*", handle_sun_overlay)

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
        schedule_store: List[Dict] = []

        def handle(route: Route) -> None:
            req = route.request
            url = req.url
            if "/api/security/schedules" in url:
                if req.method == "GET":
                    enabled = [e for e in schedule_store if e.get("enabled") is not False]
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=_json({"enabled": bool(enabled), "count": len(enabled), "entries": schedule_store}),
                    )
                    return
                body = req.post_data_json or {}
                schedule_store[:] = body.get("entries", []) if isinstance(body, dict) else []
                enabled = [e for e in schedule_store if e.get("enabled") is not False]
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=_json({"enabled": bool(enabled), "count": len(enabled), "entries": schedule_store}),
                )
                return
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
            "automation": {"auto_arm_enabled": False, "arm_away_after_s": 900, "stale_after_s": 3600, "auto_disarm_enabled": False},
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
    Used to verify stale-address visibility and the reachable-only toggle."""
    import copy
    devices = copy.deepcopy(sample_plugs)
    devices.append({
        "device_id": "plug-noip", "name": "Test NoIP", "category": "cz",
        "has_switch": True, "has_cover": False, "metered": False,
        "has_valid_ip": False, "reachable": False, "switch_on": None,
        "power_w": None, "current_ma": None, "voltage_v": None,
        "energy_kwh": None,
        "error": "No local IP — run `python -m tinytuya snapshot` on the home network, then refresh this tab.",
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
            "display_key": "mac:AA:BB:CC:DD:EE:FF",
            "display_name": None,
            "product_name": "Elgato Key Light",
            "firmware": "1.0",
            "mac_address": "AA:BB:CC:DD:EE:FF",
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
            "display_key": "192.0.2.11:9123",
            "display_name": None,
            "product_name": None,
            "firmware": None,
            "mac_address": None,
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
            parts = req.url.rstrip("/").split("/")
            verb = parts[-1]
            if verb == "refresh":
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=_json({"lights": list(store.values()), "refresh": {"safe": True}}),
                )
                return
            light_id = unquote(parts[-2] if verb == "display_name" else verb)
            light = store.get(light_id)
            if light is None:
                route.fulfill(
                    status=404,
                    content_type="application/json",
                    body=_json({"detail": "not found"}),
                )
                return
            body = req.post_data_json or {}
            if verb == "display_name":
                name = (body.get("display_name") or "").strip()
                light["display_name"] = name or None
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=_json({
                        "light_id": light_id,
                        "display_key": body.get("display_key") or light_id,
                        "display_name": name or None,
                    }),
                )
                return
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
            if verb == "refresh":
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=_json({"devices": list(store.values()), "refresh": {"safe": True}}),
                )
                return
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


@pytest.fixture
def mock_network(page: Page) -> Callable[..., Dict]:
    """Stub the Network tab API with deterministic LAN health + devices."""
    def _install(snapshot: Optional[Dict] = None, failures_before_success: int = 0) -> Dict:
        body = snapshot or {
            "internet": {
                "online": True,
                "external_ms": 14,
                "gateway_ms": 0,
                "packet_loss_pct": 0,
                "download_mbps": None,
                "upload_mbps": None,
                "speedtest_server": None,
            },
            "access_point": {
                "reachable": True,
                "model": "R9000",
                "mode": "access_point",
                "firmware": "V1.0.5.42",
                "device_count": 4,
                "error": None,
            },
            "router": {
                "reachable": True,
                "authenticated": True,
                "model": "ZXHN F6600P",
                "wan_online": True,
                "public_ip": "203.0.113.24",
                "uptime_s": 19_380,
                "error": None,
            },
            "wifi": {
                "available": True,
                "interface_name": "Wi-Fi",
                "adapter_description": "Fixture WLAN",
                "current_ssid": "TestNet-5",
                "current_bssid": "AA:BB:CC:DD:EE:01",
                "current_signal": 86,
                "current_channel": 44,
                "current_band": "5GHz",
                "current_radio_type": "802.11ac",
                "recommendations": ["Current Wi-Fi signal is strong (86%)."],
                "error": None,
                "bssids": [
                    {
                        "wifi_id": "AA:BB:CC:DD:EE:01",
                        "ssid": "TestNet-5",
                        "original_name": "TestNet-5",
                        "bssid": "AA:BB:CC:DD:EE:01",
                        "display_name": None,
                        "hidden": False,
                        "signal": 86,
                        "rssi_dbm": -57,
                        "channel": 44,
                        "band": "5GHz",
                        "radio_type": "802.11ac",
                        "authentication": "WPA2-Personal",
                        "encryption": "CCMP",
                        "connected": True,
                        "channel_width_mhz": None,
                    },
                    {
                        "wifi_id": "AA:BB:CC:DD:EE:02",
                        "ssid": "TestNet-IoT",
                        "original_name": "TestNet-IoT",
                        "bssid": "AA:BB:CC:DD:EE:02",
                        "display_name": None,
                        "hidden": False,
                        "signal": 55,
                        "rssi_dbm": -73,
                        "channel": 6,
                        "band": "2.4GHz",
                        "radio_type": "802.11n",
                        "authentication": "WPA2-Personal",
                        "encryption": "CCMP",
                        "connected": False,
                        "channel_width_mhz": None,
                    },
                ],
            },
            "alerts": ["1 wireless client(s) on weak signal (<40%)."],
            "devices": [
                {
                    "mac": "AA:00:00:00:00:01",
                    "ip": "192.0.2.11",
                    "name": "Zebra Phone",
                    "display_name": "Zebra Phone",
                    "vendor": "Apple",
                    "category": "phone",
                    "conn_type": "5GHz",
                    "is_wireless": True,
                    "signal": 30,
                    "link_rate": 300,
                    "ssid": "TestNet-5",
                    "source": "ap",
                    "online": True,
                    "important": False,
                    "hidden": False,
                    "is_new": False,
                    "randomized": False,
                    "group": None,
                    "last_conn_type": None,
                    "last_ssid": None,
                    "first_seen": 1_700_000_000,
                    "last_seen": 1_700_000_000,
                    "times_seen": 3,
                },
                {
                    "mac": "AA:00:00:00:00:02",
                    "ip": "192.0.2.12",
                    "name": "Alpha Laptop",
                    "display_name": "Alpha Laptop",
                    "vendor": "Asus",
                    "category": "computer",
                    "conn_type": "5GHz",
                    "is_wireless": True,
                    "signal": 72,
                    "link_rate": 866,
                    "ssid": "TestNet-5",
                    "source": "ap",
                    "online": True,
                    "important": False,
                    "hidden": False,
                    "is_new": False,
                    "randomized": False,
                    "group": None,
                    "last_conn_type": None,
                    "last_ssid": None,
                    "first_seen": 1_700_000_000,
                    "last_seen": 1_700_000_000,
                    "times_seen": 2,
                },
                {
                    "mac": "AA:00:00:00:00:03",
                    "ip": "192.0.2.13",
                    "name": "Kitchen Speaker",
                    "display_name": "Kitchen Speaker",
                    "vendor": "Amazon",
                    "category": "iot",
                    "conn_type": "2.4GHz",
                    "is_wireless": True,
                    "signal": 55,
                    "link_rate": 72,
                    "ssid": "TestNet-IoT",
                    "source": "ap",
                    "online": True,
                    "important": False,
                    "hidden": False,
                    "is_new": False,
                    "randomized": False,
                    "group": None,
                    "last_conn_type": None,
                    "last_ssid": None,
                    "first_seen": 1_700_000_000,
                    "last_seen": 1_700_000_000,
                    "times_seen": 1,
                },
                {
                    "mac": "AA:00:00:00:00:04",
                    "ip": "192.0.2.14",
                    "name": "NAS",
                    "display_name": "NAS",
                    "vendor": "Synology",
                    "category": "nas",
                    "conn_type": "wired",
                    "is_wireless": False,
                    "signal": None,
                    "link_rate": 1000,
                    "ssid": None,
                    "source": "ap",
                    "online": True,
                    "important": False,
                    "hidden": False,
                    "is_new": False,
                    "randomized": False,
                    "group": None,
                    "last_conn_type": None,
                    "last_ssid": None,
                    "first_seen": 1_700_000_000,
                    "last_seen": 1_700_000_000,
                    "times_seen": 1,
                },
            ],
        }
        attempts = {"count": 0}
        # Wi-Fi walk test (issue #547). The route glob below is broad enough to
        # capture /api/network/survey*, so this fixture owns those responses too
        # — otherwise the survey card would be served the LAN snapshot and read
        # an empty survey out of a shape that does not have one.
        survey: Dict = {"rooms": [], "samples": [], "known_rooms": []}
        survey_seq = {"id": 0}

        def _survey_recompute() -> None:
            rooms: Dict[str, Dict] = {}
            for sample in survey["samples"]:  # newest first
                entry = rooms.setdefault(sample["room"], {
                    "room": sample["room"],
                    "count": 0,
                    "last_recorded_at": sample["recorded_at"],
                    "last_signal": sample["signal"],
                    "last_band": sample["band"],
                    "last_ssid": sample["ssid"],
                    "last_source": sample["source"],
                    "last_found": sample["found"],
                    "last_link_rate": sample["link_rate"],
                    "last_rtt_ms": sample["rtt_ms"],
                    "last_throughput_mbps": sample["throughput_mbps"],
                    "best_signal": None,
                    "worst_signal": None,
                })
                entry["count"] += 1
                if sample["signal"] is not None:
                    best, worst = entry["best_signal"], entry["worst_signal"]
                    entry["best_signal"] = (
                        sample["signal"] if best is None else max(best, sample["signal"])
                    )
                    entry["worst_signal"] = (
                        sample["signal"] if worst is None else min(worst, sample["signal"])
                    )
            survey["rooms"] = sorted(
                rooms.values(),
                key=lambda e: (
                    0 if e["last_signal"] is None else 1,
                    e["last_signal"] if e["last_signal"] is not None else 0,
                    e["room"],
                ),
            )
            survey["known_rooms"] = sorted(rooms)

        def handle(route: Route) -> None:
            attempts["count"] += 1
            if attempts["count"] <= failures_before_success:
                route.fulfill(
                    status=503,
                    content_type="application/json",
                    body=_json({"detail": "Temporary network read failure"}),
                )
                return
            method = route.request.method.upper()
            url = route.request.url
            if "/api/network/survey" in url:
                if "/survey/payload" in url:
                    route.fulfill(
                        status=200,
                        content_type="application/octet-stream",
                        body="x" * 4096,
                    )
                    return
                if method == "GET":
                    route.fulfill(
                        status=200, content_type="application/json", body=_json(survey)
                    )
                    return
                body_json = route.request.post_data_json or {}
                if url.endswith("/survey/delete"):
                    room = (body_json.get("room") or "").strip()
                    sample_id = body_json.get("sample_id")
                    before = len(survey["samples"])
                    survey["samples"] = [
                        s for s in survey["samples"]
                        if not (room and s["room"] == room)
                        and not (sample_id is not None and s["id"] == sample_id)
                    ]
                    _survey_recompute()
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=_json({"deleted": before - len(survey["samples"])}),
                    )
                    return
                # POST /api/network/survey — the server resolves the telemetry,
                # so the stub supplies it from the fixture device list by MAC.
                mac = (body_json.get("mac") or "").strip().upper()
                device = next(
                    (d for d in body["devices"] if d["mac"].upper() == mac), None
                )
                survey_seq["id"] += 1
                sample = {
                    "id": survey_seq["id"],
                    "recorded_at": 1_700_000_000 + survey_seq["id"],
                    "room": (body_json.get("room") or "").strip(),
                    "mac": mac,
                    "signal": device["signal"] if device else None,
                    "link_rate": device["link_rate"] if device else None,
                    "band": device["conn_type"] if device else None,
                    "ssid": device["ssid"] if device else None,
                    "source": device["source"] if device else "not_found",
                    "found": device is not None,
                    "rtt_ms": body_json.get("rtt_ms"),
                    "jitter_ms": body_json.get("jitter_ms"),
                    "loss_pct": body_json.get("loss_pct"),
                    "throughput_mbps": body_json.get("throughput_mbps"),
                }
                survey["samples"].insert(0, sample)
                _survey_recompute()
                route.fulfill(
                    status=200, content_type="application/json", body=_json(sample)
                )
                return
            if method in {"PUT", "POST"}:
                body_json = route.request.post_data_json or {}
                if "/api/network/devices/" in url and url.endswith("/display_name"):
                    mac = unquote(url.split("/api/network/devices/", 1)[1].split("/", 1)[0])
                    name = (body_json.get("display_name") or "").strip()
                    for device in body["devices"]:
                        if device["mac"] == mac:
                            device["display_name"] = name or None
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=_json({"mac": mac, "display_name": name or None}),
                    )
                    return
                if "/api/network/devices/" in url and url.endswith("/hidden"):
                    mac = unquote(url.split("/api/network/devices/", 1)[1].split("/", 1)[0])
                    hidden = bool(body_json.get("hidden"))
                    for device in body["devices"]:
                        if device["mac"] == mac:
                            device["hidden"] = hidden
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=_json({"mac": mac, "hidden": hidden}),
                    )
                    return
                if "/api/network/devices/" in url and url.endswith("/group"):
                    mac = unquote(url.split("/api/network/devices/", 1)[1].split("/", 1)[0])
                    group = (body_json.get("group") or "").strip()
                    for device in body["devices"]:
                        if device["mac"] == mac:
                            device["group"] = group or None
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=_json({"mac": mac, "group": group or None}),
                    )
                    return
                if url.endswith("/api/network/groups/rename"):
                    name = (body_json.get("name") or "").strip()
                    new_name = (body_json.get("new_name") or "").strip()
                    moved = 0
                    for device in body["devices"]:
                        if (device.get("group") or "") == name:
                            device["group"] = new_name
                            moved += 1
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=_json({"name": name, "new_name": new_name, "moved": moved}),
                    )
                    return
                if url.endswith("/api/network/groups/delete"):
                    name = (body_json.get("name") or "").strip()
                    moved = 0
                    for device in body["devices"]:
                        if (device.get("group") or "") == name:
                            device["group"] = None
                            moved += 1
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=_json({"name": name, "moved": moved}),
                    )
                    return
                if url.endswith("/api/network/wifi/display_name"):
                    wifi_id = body_json.get("wifi_id")
                    name = (body_json.get("display_name") or "").strip()
                    for bssid in body["wifi"]["bssids"]:
                        if bssid["wifi_id"] == wifi_id:
                            bssid["display_name"] = name or None
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=_json({"wifi_id": wifi_id, "display_name": name or None}),
                    )
                    return
                if url.endswith("/api/network/wifi/hidden"):
                    wifi_id = body_json.get("wifi_id")
                    hidden = bool(body_json.get("hidden"))
                    for bssid in body["wifi"]["bssids"]:
                        if bssid["wifi_id"] == wifi_id:
                            bssid["hidden"] = hidden
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=_json({"wifi_id": wifi_id, "hidden": hidden}),
                    )
                    return
            route.fulfill(status=200, content_type="application/json", body=_json(body))

        page.route("**/api/network**", handle)
        return body

    return _install


def _json(obj) -> str:
    import json
    return json.dumps(obj)
