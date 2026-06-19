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
import datetime
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from typing import List, Optional

# Local imports
from app.webapp.manager import WebappManager, cert_paths, load_config
from app.tray.single_instance import SingleInstance
from src.webapp_config import append_auth_token, load_webapp_config

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Breadcrumbs for the Tailscale resolver. The tray runs under pythonw (no
# console), so logger output is void — a gitignored file log is the only way
# a future "Copy Tailscale URL" failure is diagnosable. webapp/*.log is
# already gitignored.
TS_DEBUG_LOG = PROJECT_ROOT / "webapp" / "tailscale_debug.log"


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


def _tailscale_binary() -> Optional[str]:
    """Locate the tailscale CLI — PATH first, then the standard Windows install.

    The GUI installer drops ``tailscale.exe`` under ``Program Files`` but
    doesn't always add it to PATH, and the tray is often started at login
    with a minimal environment — so PATH alone isn't enough.
    """
    found = shutil.which("tailscale")
    if found:
        return found
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Tailscale" / "tailscale.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Tailscale" / "tailscale.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _ts_debug(msg: str) -> None:
    """Append a breadcrumb to the Tailscale debug log (best-effort)."""
    logger.debug(f"tailscale: {msg}")
    try:
        TS_DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        with TS_DEBUG_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {msg}\n")
    except OSError:
        pass


def _run_tailscale(binary: str, args: List[str]) -> subprocess.CompletedProcess:
    """Run the tailscale CLI windowless, with stdin detached.

    ``CREATE_NO_WINDOW`` stops a console flashing out of the windowless
    tray; ``stdin=DEVNULL`` avoids the invalid-handle trap a ``pythonw``
    parent can hit when a child inherits a missing stdin.
    """
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        [binary, *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=12,
        check=False,
        creationflags=creationflags,
    )


def _tailscale_hostname() -> Optional[str]:
    """Return this machine's tailnet address, or None if unavailable.

    Prefers the full DNS name (e.g. ``tower.tailnet.ts.net``) — the only
    form that resolves over MagicDNS from a phone, and the form the copied
    URL wants — and falls back to the raw ``100.x`` IP. The short hostname
    is deliberately NOT used: it doesn't resolve via MagicDNS off-LAN.
    Every failure path leaves a breadcrumb in ``webapp/tailscale_debug.log``
    since the windowless tray has no console.
    """
    binary = _tailscale_binary()
    if binary is None:
        _ts_debug("CLI not found on PATH or under Program Files")
        return None
    _ts_debug(f"using binary {binary}")

    # 1. `status --json` → Self.DNSName (the FQDN).
    try:
        result = _run_tailscale(
            binary, ["status", "--self=true", "--peers=false", "--json"]
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _ts_debug(f"status raised {type(exc).__name__}: {exc}")
        result = None
    if result is not None:
        if result.returncode != 0:
            _ts_debug(
                f"status rc={result.returncode} "
                f"stderr={(result.stderr or '').strip()[:200]!r}"
            )
        else:
            try:
                data = json.loads(result.stdout)
                dns = ((data.get("Self") or {}).get("DNSName") or "").rstrip(".")
                if dns:
                    _ts_debug(f"resolved DNSName {dns}")
                    return dns
                _ts_debug(
                    f"status ok but DNSName empty; "
                    f"BackendState={data.get('BackendState')!r}"
                )
            except ValueError as exc:
                _ts_debug(f"status JSON parse failed: {exc}")

    # 2. Fallback: `tailscale ip -4` → the raw 100.x address.
    try:
        ip_res = _run_tailscale(binary, ["ip", "-4"])
        if ip_res.returncode == 0:
            lines = (ip_res.stdout or "").strip().splitlines()
            ip = lines[0].strip() if lines else ""
            if ip:
                _ts_debug(f"fell back to tailscale ip {ip}")
                return ip
        _ts_debug(
            f"ip -4 rc={ip_res.returncode} "
            f"stderr={(ip_res.stderr or '').strip()[:200]!r}"
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _ts_debug(f"ip -4 raised {type(exc).__name__}: {exc}")
    return None


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
