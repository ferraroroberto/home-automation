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
on Windows. A test marked ``desktop_only`` opts out of the WebKit run, and
so does every module listed in ``CHROMIUM_ONLY_MODULES`` (#796) and every test
marked ``chromium_only`` (#798) — see there.

``pytest_sessionfinish`` runs the vendor-verbatim leaked-browser-helper
sweep (``tests/e2e/_browser_sweep.py``, project-scaffolding #203/#204)
once the whole session — fixtures included — has torn down, so a run that
orphaned a WebKit helper reclaims it *while it is still killable*, instead
of leaving one pinning this checkout's directory (which is what makes a
later ``git worktree remove`` fail as "busy").
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, IO, Iterator, List, Optional

import pytest
from playwright.sync_api import Browser, BrowserContext, Playwright, sync_playwright

from app.webapp.event_loop import LOOP_FACTORY
from tests._progress_log import note as progress_note
from tests.e2e._browser_sweep import (
    STATE_WEDGED,
    SweepResult,
    enumerate_browser_helpers,
    sweep_browser_helpers,
)
from src.runtime_data import app_dir_env_var
from tests.e2e._e2e_live_guard import require_disposable_instance
from tests.e2e._surface import pytest_collection_modifyitems as _deselect_non_capturing

# Imported so pytest registers each API-stub fixture for every test in the suite.
from tests.e2e.fixtures.energy import mock_energy  # noqa: F401
from tests.e2e.fixtures.lights import mock_lights, sample_lights  # noqa: F401
from tests.e2e.fixtures.network import mock_network  # noqa: F401
from tests.e2e.fixtures.plugs import mock_tuya, sample_plugs, sample_plugs_with_no_ip  # noqa: F401
from tests.e2e.fixtures.presence import mock_presence  # noqa: F401
from tests.e2e.fixtures.security import mock_security  # noqa: F401
from tests.e2e.fixtures.stubs import (  # noqa: F401
    _stub_circuits_api,
    _stub_home_assistant_api,
    _stub_network_api,
)
from tests.e2e.fixtures.units import mock_api, sample_units  # noqa: F401

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
_E2E_USAGE_DB = Path(tempfile.gettempdir()) / "ha_e2e_usage.sqlite3"

# Per-file database overrides the app honours (the ``env_var=`` of each
# ``runtime_db_path`` call). Each outranks the data-directory variable, so an
# inherited one would send a test boot's writes to whatever it names (#975).
_PER_FILE_DB_ENV_VARS = frozenset({
    "ENERGY_HISTORY_DB_PATH",
    "NETWORK_HISTORY_DB_PATH",
    "NETWORK_SURVEY_DB_PATH",
})

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


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


#: Modules whose tests assert DOM state, request wiring or copy only — no
#: layout, touch target, bottom nav, safe area, iPhone width or engine-specific
#: behaviour — so the WebKit/iPhone projection re-proves nothing the Chromium
#: one did not (#796, from the #778 audit: ~46 nodes, a fifth of the suite's
#: wall time). Add a module only when that holds for every test in it; **when in doubt it stays on
#: both**. A module that carries even one layout/44px/390px test, or that
#: pins per-projection behaviour (``test_driver_isolation``,
#: ``test_helper_cwd_isolation``), does not belong here — and neither do the
#: boot canary (``test_smoke``) and the auth boundary (``test_login``), whose
#: whole point is that every engine gets through them. Deselected, never
#: deleted: ``--browser webkit`` still shows them as deselected, and removing
#: a name from this set restores the WebKit nodes with no other change.
#:
#: A module that mixes DOM-state tests with a layout test marks the DOM-state
#: ones individually with ``@pytest.mark.chromium_only`` instead (#798) — the same
#: deselection, same rule, applied per test.
CHROMIUM_ONLY_MODULES = frozenset({
    "test_ac_tab",            # pane states (loading/empty/unavailable/stale) + snapshot paint
    "test_boost_coordinator", # settings form: persistence, range refusal, summary text
    "test_circuits",          # CT-clamp group: rows, rename dialog, hide state — text and class only
    "test_lazy_libraries",    # which scripts/styles the page requests, and when — network wiring
    "test_lights",            # light rows, bulk buttons, rename — POST round-trips and states
    "test_pc_fleet",          # roster chips, toggles, wake PUT/POST wiring
    "test_pv_system",         # PV config rows, validation, forecast toast
    "test_vm_tile",           # VM tile states and start/stop command errors
    "test_voice_commands",    # language filter and bilingual card text
})


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--e2e-autoboot",
        action="store_true",
        default=False,
        help="always boot a disposable webapp, even when this checkout's own port is "
        "occupied (the tray): skips the live-instance guard and never adopts. "
        "`/design-review measure --e2e` passes it (.fleet.toml [design.review.e2e]).",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "desktop_only: skip on the WebKit/iPhone projection")
    config.addinivalue_line(
        "markers",
        "chromium_only: deselect the WebKit projection of this one test (#798) — the "
        "per-test form of CHROMIUM_ONLY_MODULES, for a DOM-state test that lives in a "
        "module that also holds a layout test",
    )
    selected: List[str] = config.option.browser
    if not selected:
        selected.extend(["chromium", "webkit"])
    _reap_orphaned_webkit_zombies()
    config.stash[_WEDGED_AT_START] = _wedged_helper_pids()


def pytest_collection_modifyitems(config: pytest.Config, items: List[pytest.Item]) -> None:
    """Drop the WebKit projection of every ``CHROMIUM_ONLY_MODULES`` module (#796)
    and of every test marked ``@pytest.mark.chromium_only`` (#798).

    Deselected rather than skipped: a skip still builds the WebKit context and
    page fixtures first, which is most of what these tests cost.

    Also runs the vendored design-capture deselection (``_surface.py``): with
    ``DESIGN_SURFACE_CAPTURE`` set, only tests that call ``surface(`` stay. It is
    called here, not imported under its own name, because a second
    ``pytest_collection_modifyitems`` in this module would replace this one.
    """
    kept: List[pytest.Item] = []
    dropped: List[pytest.Item] = []
    for item in items:
        callspec = getattr(item, "callspec", None)
        browser = callspec.params.get("browser_name") if callspec else None
        chromium_only = (
            item.module.__name__.rsplit(".", 1)[-1] in CHROMIUM_ONLY_MODULES
            or item.get_closest_marker("chromium_only") is not None
        )
        if browser == "webkit" and chromium_only:
            dropped.append(item)
        else:
            kept.append(item)
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept
    _deselect_non_capturing(config, items)


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
    progress_note(result.summary())
    for entry in result.killed:
        print(f"  reclaimed leaked helper: {entry}")
        progress_note(f"  reclaimed leaked helper: {entry}")
    line = _newly_wedged_line(session.config.stash.get(_WEDGED_AT_START, None), result)
    print(line)
    progress_note(line)


#: Wedged helper PIDs seen at session start, or ``None`` when unknown (#971).
_WEDGED_AT_START = pytest.StashKey[Optional[frozenset]]()
#: The gate (``scripts/verify-before-ship.ps1``) greps the progress log for
#: this prefix to print the count as its own state line.
NEWLY_WEDGED_PREFIX = "[e2e] browser helpers newly wedged during this run:"


def _wedged_helper_pids() -> Optional[frozenset]:
    """PIDs of browser helpers wedged inside termination right now (#971).

    Only wedged PIDs are snapshotted: a wedged process keeps its PID for good
    (nothing can reap it), so a finish-time PID absent from this set is a
    genuinely new wedge, never a recycled PID. ``None`` when the state can't
    be read (non-Windows) — unknown, not zero.
    """
    if sys.platform != "win32":
        return None
    try:
        return frozenset(p.pid for p in enumerate_browser_helpers() if p.state == STATE_WEDGED)
    except Exception:
        logger.warning("⚠️ wedged-helper snapshot failed", exc_info=True)
        return None


def _newly_wedged_line(at_start: Optional[frozenset], result: SweepResult) -> str:
    """This run's wedged-helper residue as its own advisory state (#971).

    The sweep's ``wedged:out-of-scope`` count mixes every earlier run's
    unkillable residue with this run's; the diff against the session-start
    snapshot isolates what this run added. Host-wide, so a concurrent gate
    in another checkout can contribute — the line says "during", not "by".
    Never touches ``exitstatus``: a wedge is not a test failure.
    """
    if at_start is None or not result.supported:
        return f"{NEWLY_WEDGED_PREFIX} UNKNOWN (helper state unreadable on this host)"
    new = sorted(e.process.pid for e in result.wedged if e.process.pid not in at_start)
    pids = f" (pids {', '.join(map(str, new))})" if new else ""
    return f"{NEWLY_WEDGED_PREFIX} {len(new)}{pids}"


def _autoboot_env(data_dir: Path) -> Dict[str, str]:
    """The environment for the autobooted webapp.

    ``HOME_AUTOMATION_DATA_DIR`` points every database resolved through
    ``runtime_db_path`` at a per-run temp folder (#975): per-file overrides
    cover only the files they name, so a database added later would silently
    land on the owner's real runtime folder. The per-file ``*_DB_PATH``
    variables in ``_PER_FILE_DB_ENV_VARS`` are dropped from the inherited
    environment because each one outranks the directory variable.
    """
    inherited = {
        k: v for k, v in os.environ.items() if k not in _PER_FILE_DB_ENV_VARS
    }
    return {
        **inherited,
        app_dir_env_var("home-automation"): str(data_dir),
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
        # The browser posts views and taps to /api/usage/events (#906);
        # keep a test boot's clicks out of the real usage log.
        "USAGE_LOG_DB_PATH": str(_E2E_USAGE_DB),
        # Same for the HVAC automation engine: never drive real units from a
        # test boot (the dormant-tick short-circuit makes it harmless with no
        # config, but keep it explicitly off like the sampler).
        "HVAC_AUTOMATION_ENABLED": "0",
        "SECURITY_SCHEDULES_ENABLED": "0",
        # Never move real blinds from a test boot (#871).
        "BLIND_SCHEDULES_ENABLED": "0",
        "PRESENCE_ICLOUD_REFRESH_ENABLED": "0",
        "PRESENCE_AUTOMATION_ENGINE_ENABLED": "0",
        # Assist trace ingestion is a production background read. Browser
        # tests stub /api/ha and must never inspect the live HA instance.
        "HA_TRACE_ENABLED": "0",
        # The nightly speed-test loop spends real bandwidth; a test boot
        # must never start it, whatever a copied config says (#840).
        "SPEEDTEST_SCHEDULE_ENABLED": "0",
        # The master switch (#876): only the tray / webapp.bat opt in, so
        # a test boot runs no engine even when the shell running pytest
        # inherited the opt-in. The per-engine flags above stay as depth.
        "HOME_AUTOMATION_ENGINES": "0",
    }


@pytest.fixture(scope="session")
def base_url(request: pytest.FixtureRequest) -> Iterator[str]:
    # Vendored guard (issue #538): refuses via pytest.exit if :8447 is
    # occupied and E2E_LIVE isn't set. This repo's caller-side choice on an
    # opt-in hit is read-only adoption of the live tray, never a restart or
    # kill — see the module docstring above for why. `--e2e-autoboot` is the
    # explicit "boot my own, leave the tray alone" form: no guard, no adoption.
    if request.config.getoption("--e2e-autoboot"):
        live_opt_in = False
    else:
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
    # Per-run runtime-data folder (#975); removed in the teardown below.
    data_dir = Path(tempfile.mkdtemp(prefix="ha_e2e_data_"))
    kwargs: dict = dict(
        cwd=str(_REPO_ROOT), stdout=log_handle, stderr=subprocess.STDOUT,
        env=_autoboot_env(data_dir),
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
        shutil.rmtree(data_dir, ignore_errors=True)


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

    The watchdog is downstream of the wedge, not its cause (#971, measured on
    Playwright 1.60 / webkit-2287): a per-context ``WebKitNetworkProcess``
    wedged inside termination at an ordinary per-test ``context.close()``
    mid-run, and the *next* ``browser.close()`` hung waiting on it. When it
    fires, it leaves a line in the gate's progress log.
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
        # The warning above only reaches pytest's capture; the gate log is
        # where a wedged-helper investigation starts (#971).
        progress_note(
            f"teardown watchdog fired: {label} did not complete within "
            f"{_TEARDOWN_TIMEOUT_S:.0f}s, force-killing driver pid {driver_pid}"
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


@contextmanager
def _neutral_driver_cwd() -> Iterator[str]:
    """Run the enclosed block with this process's cwd outside the checkout (#681).

    Everything Playwright spawns inherits a working directory: the Node driver
    takes this process's (``playwright/_impl/_transport.py`` passes no ``cwd``),
    each browser takes the driver's, and each helper — ``WebKitWebProcess``,
    ``WebKitGPUProcess``, ``WebKitNetworkProcess`` — takes the browser's. Run
    the suite from ``home-automation-wt-<N>`` and that whole tree roots itself
    in the worktree, and **Windows will not delete a directory that is a live
    process's cwd**. That is what left six empty, undeletable worktrees on the
    fleet host between 2026-08-19 and 2026-08-22.

    It matters here and nowhere else in the fleet because of
    ``_bounded_teardown`` above: when ``browser.close()`` overruns, it
    ``taskkill /F /T``s the driver tree, which kills the browser out from under
    its helpers. A helper killed that way can wedge *inside* termination —
    ``ExitStatus`` set (so ``taskkill`` answers "no running instance" and
    ``GetExitCodeProcess`` reports a clean 0), yet ``GetProcessTimes`` reports
    no exit time, a thread still alive, and the cwd handle still held. (#971
    later caught the same wedge arising at a plain per-test context close,
    before any kill.) It can never be reaped, so the directory it pins is
    pinned for good. #440 knew the
    watchdog leaked helpers; what it could not know is that the leak is
    unkillable, which is why every mitigation aimed at *reaping* them (#480's
    startup sweep, #583's ``_browser_sweep``) was aiming at the wrong half.

    So don't fight the wedge — make it harmless. With the driver started from
    ``%TEMP%`` no helper ever roots in the checkout, and a wedged one pins a
    directory nobody wants to delete. ``gettempdir()`` deliberately, **not**
    ``mkdtemp()``: a per-run temp directory would become unremovable by the
    exact mechanism this exists to avoid.

    Only the driver's *spawn* needs the neutral cwd — the driver's own working
    directory is fixed at spawn and is what every later ``launch()`` inherits —
    so this restores the caller's cwd immediately, and pytest carries on from
    the checkout as before. ``tests/e2e/test_helper_cwd_isolation.py`` pins it.
    """
    original = os.getcwd()
    neutral = tempfile.gettempdir()
    os.chdir(neutral)
    try:
        yield neutral
    finally:
        os.chdir(original)


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

    Started from a neutral cwd so nothing it spawns can pin this checkout —
    see `_neutral_driver_cwd` (#681).
    """
    with _neutral_driver_cwd():
        pw = sync_playwright().start()
    yield pw
    _bounded_teardown(pw.stop, f"playwright.stop() [{browser_name}]", _driver_pid(pw))


@pytest.fixture(scope="session")
def browser(
    launch_browser: Callable[..., Browser], playwright: Playwright, browser_name: str
) -> Iterator[Browser]:
    browser_instance = launch_browser()
    yield browser_instance
    _bounded_teardown(
        browser_instance.close, f"browser.close() [{browser_name}]", _driver_pid(playwright)
    )


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
