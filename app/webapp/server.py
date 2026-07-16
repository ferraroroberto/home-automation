r"""FastAPI webapp — mobile-first MELCloud Home control dashboard.

Routes are split across ``app/webapp/routers/`` — one module per mount,
listed below in the same order they're mounted in ``create_app()``. Each
module's own docstring documents its exact endpoints; kept as one line per
module here (not a per-endpoint table) so this list can't drift out of sync
with the router set the way the old exhaustive table did (#453).

    misc               → /, /healthz, /api/version
    (static mount)     → /static/*                        (StaticFiles, mounted in create_app — not a router)
    auth               → /api/login
    units              → /api/units*                    (MELCloud Home units)
    energy             → /api/energy*                   (SMA flow/history/cost/forecast)
    weather            → /api/weather
    tuya               → /api/tuya*                      (Smart Life / Tuya plugs, blinds)
    ups                → /api/ups*                        (USB UPS + notify prefs)
    lights             → /api/lights*                     (Elgato lights)
    cameras            → /api/cameras*                    (RTSP/ONVIF, snapshot, stream, PTZ, presets)
    security           → /api/security, /api/security/events, /api/security/{action}, zones
    security_schedules → /api/security/schedules
    security_scene     → /api/security/scene-pairings
    security_override  → /api/security/overrides
    security_notify    → /api/security/notify-prefs
    network            → /api/network*                    (LAN health, AP/router reboot, device/wifi hide, wifi rename)
    dhcp_plan          → /api/network/dhcp-plan*, /dhcp-bindings*, /dhcp-overrides*, /dhcp-reservations*
    presence           → /api/presence*, /api/location*
    push               → /api/push*                       (browser Web Push)
    hyperv             → /api/hyperv*                      (Home Assistant VM status/start/stop)
    ha                 → /api/ha*                          (Voice PE rooms, push-to-talk, transcribe proxy)
    activity           → /api/activity*                    (unified event log)
    nav_debug          → /api/nav-debug                    (client nav-pin diagnostics)
    wake_alarms        → /api/wake-alarms*, /api/wake-timers*

Run with::

    & .\.venv\Scripts\python.exe -m uvicorn app.webapp.server:app --host 0.0.0.0 --port 8447
    .\webapp.bat                                              # HTTPS when cert present
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiohttp
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from app.webapp.middleware import BearerTokenMiddleware
from src.camera_token import verify as _verify_camera_token
from app.webapp.routers import activity, auth, cameras, dhcp_plan, energy, ha, hyperv, lights, misc, nav_debug, network, presence, push, security, security_notify, security_override, security_schedules, security_scene, tuya, units, ups, voice_commands, wake_alarms, weather
from app.webapp.routers._helpers import BUILD_INFO, STATIC_DIR
from app.webapp.automation import start_automation
from app.webapp.power_monitor import start_power_monitor
from app.webapp.presence_automation import start_presence_automation
from app.webapp.presence_refresher import start_presence_refresher
from app.webapp.security_automation import start_security_schedules
from app.webapp.wake_alarm_automation import start_wake_alarms
from app.webapp.sampler import start_sampler
from app.webapp.telemetry_sampler import start_telemetry_sampler
from app.webapp.ha_trace_collector import start_ha_trace_collector
from src import telemetry
from src.push_notifications import validate_push_config
from src.webapp_config import load_webapp_config

logger = logging.getLogger(__name__)

# Hash-stamped assets get a one-year immutable cache: the fleet hash in
# the query string makes the URL change on every edit, so a stale copy
# can never be served. Icons + manifest revalidate daily — they almost
# never change but we don't want a year of staleness either.
_LONG_CACHE = "public, max-age=31536000, immutable"
_DAY_CACHE = "public, max-age=86400"
_IMMUTABLE_SUFFIXES = frozenset({".js", ".css"})
_DAILY_SUFFIXES = frozenset({".webmanifest", ".png", ".ico"})


class CachingStaticFiles(StaticFiles):
    """``StaticFiles`` with per-file ``Cache-Control`` + JS-import stamping.

    Starlette's mount serves every file with only ``ETag`` /
    ``Last-Modified``, leaving iOS Safari free to heuristic-cache the
    module graph and the PWA shell. This subclass stamps an explicit
    policy keyed on the suffix, and rewrites each served ``.js`` module's
    relative ``import`` URLs to carry the build fleet hash — so an edit to
    any module busts the whole graph.
    """

    def file_response(
        self,
        full_path: "os.PathLike[str]",
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        path = Path(full_path)
        suffix = path.suffix.lower()

        if suffix == ".js":
            try:
                body = path.read_text(encoding="utf-8")
            except OSError:
                return super().file_response(
                    full_path, stat_result, scope, status_code
                )
            media_type, _ = mimetypes.guess_type(str(path))
            return Response(
                content=BUILD_INFO.stamp_js(body, path),
                status_code=status_code,
                media_type=media_type or "text/javascript",
                headers={"Cache-Control": _LONG_CACHE},
            )

        response = super().file_response(
            full_path, stat_result, scope, status_code
        )
        if suffix in _IMMUTABLE_SUFFIXES:
            response.headers["Cache-Control"] = _LONG_CACHE
        elif suffix in _DAILY_SUFFIXES:
            response.headers["Cache-Control"] = _DAY_CACHE
        return response


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own background tasks for the process life."""
    # Initialize the unified telemetry store first, so producers (alarm/power/
    # presence/plug/RISCO) mirror their events into it from the first request
    # (#289). Done in lifespan (not create_app) so importing the app for tests
    # never touches the real DB; the API test layer points it at a temp DB.
    try:
        telemetry.init_db()
    except Exception as exc:  # noqa: BLE001 — telemetry is non-critical to serving
        logger.warning("⚠️  Telemetry store init failed (events disabled): %s", exc)

    # Validate the VAPID private key once at boot rather than discovering it's
    # unreadable on the first presence/power/alarm push (#284) — logs its own
    # clear warning and is non-critical to serving either way.
    try:
        validate_push_config()
    except Exception as exc:  # noqa: BLE001 — push validation is non-critical to serving
        logger.warning("⚠️  Web Push config validation failed: %s", exc)

    # One shared outbound pool for request-driven HA + Voice Transcriber calls.
    # In particular, live dictation sends a chunk every second; reusing this
    # pool avoids constructing a new client/socket pool for every chunk.
    app.state.outbound_http = aiohttp.ClientSession()

    tasks = [
        t for t in (
            start_sampler(),
            start_telemetry_sampler(),
            start_automation(),
            start_presence_refresher(),
            start_presence_automation(),
            start_security_schedules(),
            start_wake_alarms(),
            start_power_monitor(),
            start_ha_trace_collector(),
        )
        if t is not None
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await app.state.outbound_http.close()


def create_app() -> FastAPI:
    """Build the FastAPI app, wired with config + auth + routers."""
    webapp_cfg = load_webapp_config()
    auth.ensure_auth_log_handler()

    app = FastAPI(title="Home Automation", version="0.1.0", lifespan=lifespan)

    # Read the token from app.state on every request so a future rotate
    # could take effect without re-importing the module.
    app.add_middleware(
        BearerTokenMiddleware,
        get_token=lambda: getattr(app.state.webapp_config, "auth_token", ""),
        verify_camera_token=lambda tok: _verify_camera_token(
            tok, getattr(app.state.webapp_config, "auth_token", "")
        ),
    )

    app.state.webapp_config = webapp_cfg

    if STATIC_DIR.exists():
        app.mount(
            "/static",
            CachingStaticFiles(directory=str(STATIC_DIR)),
            name="static",
        )

    app.include_router(misc.router)
    app.include_router(auth.router)
    app.include_router(units.router)
    app.include_router(energy.router)
    app.include_router(weather.router)
    app.include_router(tuya.router)
    app.include_router(ups.router)
    app.include_router(lights.router)
    app.include_router(cameras.router)
    app.include_router(security.router)
    app.include_router(security_schedules.router)
    app.include_router(security_scene.router)
    app.include_router(security_override.router)
    app.include_router(security_notify.router)
    app.include_router(network.router)
    app.include_router(dhcp_plan.router)
    app.include_router(presence.router)
    app.include_router(push.router)
    app.include_router(hyperv.router)
    app.include_router(ha.router)
    app.include_router(activity.router)
    app.include_router(nav_debug.router)
    app.include_router(wake_alarms.router)
    app.include_router(voice_commands.router)

    logger.info(
        "ℹ️  webapp build %s (fleet %s) built %s",
        BUILD_INFO.git_sha,
        BUILD_INFO.fleet_hash or "missing",
        BUILD_INFO.built_at,
    )
    logger.info(
        "ℹ️  webapp ready (auth gate %s)",
        "ON" if webapp_cfg.auth_token else "OFF",
    )
    return app


# Module-level app for `uvicorn app.webapp.server:app`.
app = create_app()
