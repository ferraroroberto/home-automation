"""Webapp process manager — adopt-or-spawn for uvicorn.

Adapted from the canonical fleet manager (photo-ocr / voice-transcriber):

- ``status()`` checks ``GET /healthz`` and a low-level TCP probe.
- ``start()`` adopts an already-listening uvicorn (no second spawn) or
  spawns ``python -m uvicorn app.webapp.server:app`` from this venv.
- ``stop()`` only terminates a process this manager spawned. An
  externally started uvicorn is left alone.

Used by the tray so launching ``tray.bat`` brings up the webapp.
Standalone ``webapp.bat`` is the "server only, no tray" alternative.

Two adaptations to this repo's shape vs the canonical sister managers:

* Host/port are read from this repo's existing
  :func:`src.webapp_config.load_webapp_config`, not a separate AppConfig.
* The readiness probe uses stdlib ``urllib`` + an unverified SSL context
  rather than ``requests``, so the tray adds only the two dependencies the
  issue calls for (``pystray`` + ``Pillow``).
"""

from __future__ import annotations

# Standard library imports
import logging
import os
import signal
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

# Local imports
from app.tray.single_instance import cross_process_lock
from src.webapp_config import load_webapp_config

logger = logging.getLogger(__name__)

OWNERSHIP_NONE = "none"
OWNERSHIP_OURS = "ours"
OWNERSHIP_EXTERNAL = "external"

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass(frozen=True)
class WebappManagerConfig:
    """Runtime knobs; host/port come from config/webapp_config.json."""

    host: str = "0.0.0.0"
    port: int = 8447
    startup_timeout_seconds: float = 15.0
    request_timeout_seconds: float = 1.0
    poll_interval_seconds: float = 0.4


@dataclass
class WebappStatus:
    running: bool
    ownership: str
    pid: Optional[int]
    port: int
    base_url: str  # https://… when cert exists, http://… otherwise
    detail: str


def load_config() -> WebappManagerConfig:
    """Build the manager config from the repo's webapp_config loader."""
    cfg = load_webapp_config()
    return WebappManagerConfig(host=cfg.host, port=cfg.port)


def cert_paths(project_root: Optional[Path] = None) -> Optional[tuple[Path, Path]]:
    root = project_root or PROJECT_ROOT
    cert = root / "webapp" / "certificates" / "cert.pem"
    key = root / "webapp" / "certificates" / "key.pem"
    if cert.exists() and key.exists():
        return cert, key
    return None


def _renew_tailscale_cert() -> None:
    """Best-effort auto-renew of the Tailscale (Let's Encrypt) cert before spawn.

    Mirrors the ``webapp.bat`` ``--check`` hook so the tray-owned boot path also
    self-heals a cert expiring within 30 days. No-op when the cert is missing or
    is not a ``.ts.net`` cert; never raises, so a renewal hiccup can't block the
    webapp from starting.
    """
    script = PROJECT_ROOT / "scripts" / "gen_tailscale_cert.py"
    if not script.exists():
        return
    try:
        subprocess.run(
            [sys.executable, str(script), "--check"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("⚠️  Tailscale cert renew check failed: %s", exc)


def _probe_url(scheme: str, host: str, port: int) -> str:
    return f"{scheme}://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}"


class WebappManager:
    """Start / stop / health-check the webapp uvicorn process."""

    def __init__(self, config: Optional[WebappManagerConfig] = None) -> None:
        self.config = config or WebappManagerConfig()
        self._proc: Optional[subprocess.Popen] = None
        # Verification is off for the loopback health probe regardless of cert
        # source (we are the server, not a remote client; the Tailscale cert is
        # issued for the .ts.net name, not 127.0.0.1).
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE

    @property
    def base_url(self) -> str:
        scheme = "https" if cert_paths() else "http"
        return _probe_url(scheme, self.config.host, self.config.port)

    def is_reachable(self) -> bool:
        for scheme in ("https", "http"):
            url = _probe_url(scheme, self.config.host, self.config.port) + "/healthz"
            try:
                ctx = self._ssl_ctx if scheme == "https" else None
                with urllib.request.urlopen(
                    url, timeout=self.config.request_timeout_seconds, context=ctx
                ) as resp:
                    if resp.status == 200:
                        return True
            except (urllib.error.URLError, OSError, ValueError):
                continue
        return False

    def is_port_in_use(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            host = self.config.host if self.config.host != "0.0.0.0" else "127.0.0.1"
            return s.connect_ex((host, self.config.port)) == 0

    def status(self) -> WebappStatus:
        running_here = self._proc is not None and self._proc.poll() is None
        reachable = self.is_reachable() or self.is_port_in_use()

        if running_here and reachable:
            return WebappStatus(
                running=True,
                ownership=OWNERSHIP_OURS,
                pid=self._proc.pid,
                port=self.config.port,
                base_url=self.base_url,
                detail="running (started by this process)",
            )
        if reachable:
            return WebappStatus(
                running=True,
                ownership=OWNERSHIP_EXTERNAL,
                pid=None,
                port=self.config.port,
                base_url=self.base_url,
                detail="running (external — adopted)",
            )
        return WebappStatus(
            running=False,
            ownership=OWNERSHIP_NONE,
            pid=None,
            port=self.config.port,
            base_url=self.base_url,
            detail="not running",
        )

    def start(self, wait: bool = True) -> WebappStatus:
        # Race-safe adopt-or-spawn (project-scaffolding#39): serialize the
        # status()-then-Popen critical section across processes so two trays
        # starting at once cannot both spawn uvicorn. The loser blocks, then
        # re-checks below and adopts the now-listening webapp. The lock is held
        # through _wait_until_ready so a serialized caller sees a bound port.
        # cross_process_lock fails open (Windows mutex glitch / non-Windows), so
        # it never blocks startup. Vendored byte-identical from the scaffold.
        with cross_process_lock(rf"Global\home-automation-webapp-start-{self.config.port}"):
            current = self.status()
            if current.running and current.ownership == OWNERSHIP_OURS:
                logger.info(f"ℹ️  Webapp already {current.detail}")
                return current
            if current.running:
                logger.info(f"🔗 Adopting external webapp at {current.base_url}")
                return current

            _renew_tailscale_cert()
            cmd = self._build_command()
            logger.info(f"🚀 Starting webapp: {' '.join(cmd)}")

            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"

            try:
                popen_kwargs: Dict[str, Any] = dict(
                    cwd=str(PROJECT_ROOT),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=env,
                )
                if sys.platform == "win32":
                    popen_kwargs["creationflags"] = (
                        subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                    )
                self._proc = subprocess.Popen(cmd, **popen_kwargs)
            except FileNotFoundError as exc:
                raise RuntimeError(f"❌ python launcher not found: {exc}") from exc
            except Exception as exc:
                raise RuntimeError(f"❌ failed to launch webapp: {exc}") from exc

            if wait:
                self._wait_until_ready()
            return self.status()

    def restart(self, wait: bool = True) -> WebappStatus:
        """Stop (if we own it) and start again. Used by the tray to pick up code changes."""
        status = self.status()
        if status.running and status.ownership == OWNERSHIP_EXTERNAL:
            raise RuntimeError(
                "Webapp is running but was started externally — cannot restart from here"
            )
        if status.running:
            self.stop()
        return self.start(wait=wait)

    def stop(self) -> WebappStatus:
        status = self.status()
        if status.ownership == OWNERSHIP_EXTERNAL:
            logger.info("✋ Leaving external webapp running (not ours)")
            return status
        if not status.running or self._proc is None:
            return status

        p = self._proc
        logger.info(f"🛑 Stopping webapp (pid={p.pid})")
        try:
            if sys.platform == "win32":
                try:
                    p.send_signal(signal.CTRL_BREAK_EVENT)
                except Exception as exc:
                    logger.debug(f"CTRL_BREAK_EVENT failed: {exc}")
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=3)
        finally:
            self._proc = None

        return WebappStatus(
            running=False,
            ownership=OWNERSHIP_NONE,
            pid=None,
            port=self.config.port,
            base_url=self.base_url,
            detail="stopped",
        )

    def _build_command(self) -> List[str]:
        py = sys.executable
        cmd: List[str] = [
            py,
            "-m",
            "uvicorn",
            "app.webapp.server:app",
            "--host",
            self.config.host,
            "--port",
            str(self.config.port),
            "--log-level",
            "warning",
        ]
        certs = cert_paths()
        if certs is not None:
            cert, key = certs
            cmd.extend([
                "--ssl-keyfile",
                str(key),
                "--ssl-certfile",
                str(cert),
            ])
        return cmd

    def _wait_until_ready(self) -> None:
        deadline = time.time() + self.config.startup_timeout_seconds
        while time.time() < deadline:
            if self._proc is None or self._proc.poll() is not None:
                raise RuntimeError("❌ webapp uvicorn exited before becoming ready")
            if self.is_reachable():
                logger.info(f"✅ Webapp ready at {self.base_url}")
                return
            time.sleep(self.config.poll_interval_seconds)
        raise RuntimeError(
            f"❌ webapp did not become ready within "
            f"{self.config.startup_timeout_seconds}s"
        )
