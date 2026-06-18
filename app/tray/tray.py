"""System-tray launcher — owns the webapp lifecycle.

Mobile-first design means there's no real desktop UI to surface; the
tray exists so launching ``tray.bat`` brings the webapp up alongside
Windows login without keeping a console window open.

Menu:
    Open home automation       — open the local URL in the default browser
    Copy local URL             — clipboard the local URL (with ?token=…)
    Copy Tailscale URL         — clipboard https://<tailscale-host>:8447?token=…
    Restart webapp             — stop + start so a new pull is picked up
    Status                     — popup with webapp state
    --
    Quit                       — stop the webapp and exit
"""

from __future__ import annotations

# Standard library imports
import json
import logging
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Optional

# Local imports
from app.tray.manager import WebappManager, cert_paths, load_config
from app.tray.single_instance import SingleInstance
from src.webapp_config import append_auth_token, load_webapp_config

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _build_icon():
    """Lazy import Pillow so plain CLI use doesn't drag it in."""
    from PIL import Image
    icon_path = PROJECT_ROOT / "app" / "webapp" / "static" / "icon-512.png"
    if icon_path.exists():
        return Image.open(icon_path)
    # Fallback: a tiny solid block.
    return Image.new("RGB", (32, 32), (74, 138, 243))


def _clipboard_copy(text: str) -> bool:
    """Best-effort Windows clipboard. Returns True on success."""
    if sys.platform == "win32":
        try:
            p = subprocess.run(
                ["clip"],
                input=text,
                text=True,
                check=False,
                encoding="utf-8",
            )
            return p.returncode == 0
        except OSError as exc:
            logger.debug(f"clip failed: {exc}")
    return False


def _tailscale_hostname() -> Optional[str]:
    """Return the tailnet hostname for this machine, or None if unavailable."""
    try:
        result = subprocess.run(
            ["tailscale", "status", "--self=true", "--peers=false", "--json"],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        logger.debug(f"tailscale lookup failed: {exc}")
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None
    self_node = data.get("Self") or {}
    dns = (self_node.get("DNSName") or "").rstrip(".")
    if not dns:
        return None
    short = dns.split(".")[0]
    return short or dns


def _notify(title: str, message: str) -> None:
    """Surface a tray message. pythonw has no console, so this also logs."""
    logger.info(f"🔔 {title}: {message}")


def run_tray() -> int:
    """Run the tray icon. Returns when the user picks Quit."""
    try:
        import pystray  # type: ignore
        from pystray import Menu, MenuItem
    except ImportError as exc:
        logger.error(
            f"❌ pystray not installed ({exc}); install via `pip install -r requirements.txt`"
        )
        return 1

    # In-process single-instance guard (project-scaffolding#39): the tray.bat CIM
    # pre-check can let two near-simultaneous launches through, so the guarantee
    # must live in the process. Held for the tray's lifetime; the OS frees the
    # named mutex on exit. `instance` is intentionally kept referenced (quit).
    instance = SingleInstance(r"Global\home-automation-tray")
    if not instance.acquired:
        logger.info("ℹ️  Another home-automation tray is already running; exiting.")
        return 0

    manager = WebappManager(load_config())

    # Kick off the webapp on a background thread so the tray comes up
    # quickly even if uvicorn takes a second to start.
    starter_error: dict = {"exc": None}

    def _start():
        try:
            manager.start(wait=True)
            _notify("Home Automation webapp ready", manager.base_url)
        except Exception as exc:  # noqa: BLE001
            starter_error["exc"] = exc
            logger.error(f"❌ webapp start failed: {exc}")
            _notify("Home Automation start failed", str(exc))

    threading.Thread(target=_start, daemon=True).start()

    def open_local(icon, item):  # noqa: ARG001
        webbrowser.open(manager.base_url)

    def copy_local(icon, item):  # noqa: ARG001
        webapp_cfg = load_webapp_config()
        url = append_auth_token(manager.base_url, webapp_cfg.auth_token)
        if _clipboard_copy(url):
            _notify("Copied local URL", url)
        else:
            _notify("Local URL", url)

    def copy_tailscale(icon, item):  # noqa: ARG001
        host = _tailscale_hostname()
        if not host:
            _notify(
                "Tailscale not available",
                "Couldn't resolve a tailnet hostname (is `tailscale` installed and logged in?).",
            )
            return
        scheme = "https" if cert_paths() else "http"
        url = f"{scheme}://{host}:{manager.config.port}"
        webapp_cfg = load_webapp_config()
        url = append_auth_token(url, webapp_cfg.auth_token)
        if _clipboard_copy(url):
            _notify("Copied Tailscale URL", url)
        else:
            _notify("Tailscale URL", url)

    def restart_webapp(icon, item):  # noqa: ARG001
        def _do_restart():
            try:
                _notify("Home Automation", "Restarting webapp…")
                manager.restart(wait=True)
                _notify("Home Automation webapp restarted", manager.base_url)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"❌ webapp restart failed: {exc}")
                _notify("Restart failed", str(exc))

        threading.Thread(target=_do_restart, daemon=True).start()

    def show_status(icon, item):  # noqa: ARG001
        s = manager.status()
        _notify("Home Automation status", f"{s.detail} · {s.base_url}")

    def quit_app(icon, item):  # noqa: ARG001
        logger.info("👋 Tray quit requested")
        try:
            manager.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"⚠️  stop failed: {exc}")
        instance.release()
        icon.stop()

    def on_left_click(icon, item):  # noqa: ARG001
        webbrowser.open(manager.base_url)

    menu = Menu(
        MenuItem("🏠 Open home automation", on_left_click, default=True),
        MenuItem("📋 Copy local URL", copy_local),
        MenuItem("📋 Copy Tailscale URL", copy_tailscale),
        Menu.SEPARATOR,
        MenuItem("🔄 Restart webapp", restart_webapp),
        MenuItem("ℹ️ Status", show_status),
        Menu.SEPARATOR,
        MenuItem("🚪 Quit", quit_app),
    )

    icon = pystray.Icon(
        "home-automation",
        icon=_build_icon(),
        title="Home Automation",
        menu=menu,
    )
    icon.run()
    if starter_error["exc"] is not None:
        return 1
    return 0
