"""SearXNG (voice assistant Tier-3 web-search backend) status + start control.

UI-free core. Shells out to ``docker`` for the single ``searxng`` container
(the compose stack lives in the sister ``local-llm-hub`` repo, path given by
``SEARXNG_COMPOSE_PATH``) and does a lightweight HTTP ``/healthz`` probe
against ``SEARXNG_URL`` to distinguish "container up" from "actually
answering queries" — mirrors the ``src/devices/hyperv_client.py`` / ``ups_client``
"shell out, flatten, partial data stays 200" shape, including the
shared ``NO_WINDOW`` guard (``src/_no_window.py``) so no console window pops on
each Home-view poll.

Status reads degrade — a missing container, a stopped one, or an
unreachable ``/healthz`` all come back as an ``available=False``
:class:`SearxngState` with a distinct ``error``. A ``docker inspect`` that
times out is a fact not established, not a state: it raises
:class:`SearxngCommandError`, never folds into "not found". ``start_searxng``
raises :class:`SearxngConfigError` / :class:`SearxngCommandError`, so the
router can map each cause to its own HTTP status.
"""

from __future__ import annotations

import logging
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from src._no_window import NO_WINDOW

logger = logging.getLogger("searxng")

load_dotenv()

_CONTAINER_NAME = "searxng"
_COMPOSE_PATH_ENV = "SEARXNG_COMPOSE_PATH"
_URL_ENV = "SEARXNG_URL"
# Loopback, not a real machine's LAN address — an unset
# SEARXNG_URL now probes nothing reachable instead of a specific household
# host; set the env var (see .env.example) to point at the real container.
_DEFAULT_URL = "http://127.0.0.1:8085"
# ``up -d`` may pull an image; ``inspect`` only reads local daemon state.
_COMPOSE_TIMEOUT_S = 60
_INSPECT_TIMEOUT_S = 10


class SearxngConfigError(RuntimeError):
    """``SEARXNG_COMPOSE_PATH`` is unset/empty — the core can't know which stack to start."""


class SearxngCommandError(RuntimeError):
    """A ``docker`` call failed or did not return within its timeout."""


class SearxngPhase(str, Enum):
    """Where the SearXNG container stands, as every reader sees it.

    ``SERVING``: running and answering ``/healthz``. ``UNREACHABLE``: running
    but not answering (booting, or wedged). ``DOWN``: the container exists and
    is not running (exited, created, paused, dead, ...). ``NOT_FOUND``: no such
    container. Docker's own status word stays on the state as
    ``container_status`` for display; nothing else compares it.
    """

    SERVING = "serving"
    UNREACHABLE = "unreachable"
    DOWN = "down"
    NOT_FOUND = "not_found"


_DOCKER_RUNNING = "running"
_DOCKER_NOT_FOUND = "not_found"


def derive_searxng_phase(container_status: Optional[str], reachable: bool) -> SearxngPhase:
    """The one mapping from Docker's status word and the ``/healthz`` probe to a phase."""
    if container_status == _DOCKER_RUNNING:
        return SearxngPhase.SERVING if reachable else SearxngPhase.UNREACHABLE
    if container_status in (None, _DOCKER_NOT_FOUND):
        return SearxngPhase.NOT_FOUND
    return SearxngPhase.DOWN


@dataclass(frozen=True)
class SearxngState:
    """Flattened SearXNG container status for the Home Assistant card."""

    available: bool
    container_status: Optional[str] = None
    reachable: bool = False
    url: Optional[str] = None
    error: Optional[str] = None
    updated_at: Optional[str] = None

    @property
    def phase(self) -> SearxngPhase:
        return derive_searxng_phase(self.container_status, self.reachable)

    def to_dict(self) -> Dict[str, Any]:
        return {**asdict(self), "phase": self.phase.value}


def searxng_url() -> str:
    """The configured SearXNG base URL, or the hub-PC default."""
    return (os.getenv(_URL_ENV) or _DEFAULT_URL).rstrip("/")


def compose_path() -> str:
    """The configured docker-compose.yml path, or raise :class:`SearxngConfigError`."""
    path = (os.getenv(_COMPOSE_PATH_ENV) or "").strip()
    if not path:
        raise SearxngConfigError(
            f"{_COMPOSE_PATH_ENV} is not set — add the SearXNG docker-compose.yml path to .env."
        )
    return path


def fetch_searxng_state() -> SearxngState:
    """Read the container's live status, degrading to ``available=False``.

    Raises :class:`SearxngCommandError` only when ``docker inspect`` times out.
    """
    url = searxng_url()
    status = _container_status()
    reachable = _probe_healthz(url) if status == _DOCKER_RUNNING else False
    phase = derive_searxng_phase(status, reachable)
    available = phase is SearxngPhase.SERVING

    error = None
    if phase is SearxngPhase.NOT_FOUND:
        error = "SearXNG container not found — start it from this card or `start_searxng.bat`."
    elif phase is SearxngPhase.DOWN:
        error = f"SearXNG container is {status}."
    elif phase is SearxngPhase.UNREACHABLE:
        error = "Container is running but not answering /healthz yet."

    return SearxngState(
        available=available,
        container_status=status,
        reachable=reachable,
        url=url,
        error=error,
        updated_at=_now(),
    )


def start_searxng() -> SearxngState:
    """``docker compose up -d`` the stack (idempotent) and return the read-back state."""
    return _compose(["up", "-d"], "docker compose up failed", "start")


def restart_searxng() -> SearxngState:
    """``docker compose restart`` the stack and return the read-back state.

    ``up -d`` is a no-op against a container Docker already considers running,
    so it cannot recover the one failure shape no restart policy catches
    either: up but wedged, not answering ``/healthz``. Only the
    watchdog calls this, and only after the unreachable state has persisted
    past its grace window — a freshly-booting container must never be
    recreated out from under itself.
    """
    return _compose(["restart"], "docker compose restart failed", "restart")


def _compose(args: List[str], fallback_err: str, label: str) -> SearxngState:
    """Run one ``docker compose`` subcommand against the stack, then read state back."""
    path = compose_path()  # SearxngConfigError propagates → router maps to 503
    try:
        result = subprocess.run(
            ["docker", "compose", "-f", path, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=_COMPOSE_TIMEOUT_S,
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        err = f"docker compose {label} timed out after {_COMPOSE_TIMEOUT_S} s"
        logger.warning("⚠️  SearXNG %s", err)
        raise SearxngCommandError(err) from None
    if result.returncode != 0:
        err = (result.stderr or result.stdout or fallback_err).strip()
        logger.warning("⚠️  SearXNG %s failed: %s", label, err)
        raise SearxngCommandError(_short(err))
    return fetch_searxng_state()


def _container_status() -> str:
    try:
        result = subprocess.run(
            ["docker", "inspect", _CONTAINER_NAME, "--format", "{{.State.Status}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=_INSPECT_TIMEOUT_S,
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        raise SearxngCommandError(
            f"docker inspect timed out after {_INSPECT_TIMEOUT_S} s"
        ) from None
    if result.returncode != 0:
        return _DOCKER_NOT_FOUND
    return (result.stdout or "").strip() or "unknown"


def _probe_healthz(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/healthz", timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _short(text: str, limit: int = 200) -> str:
    return " ".join((text or "").split())[:limit]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
