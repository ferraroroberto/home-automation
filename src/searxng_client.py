"""SearXNG (voice assistant Tier-3 web-search backend) status + start control
(issue #321).

UI-free core. Shells out to ``docker`` for the single ``searxng`` container
(the compose stack lives in the sister ``local-llm-hub`` repo, path given by
``SEARXNG_COMPOSE_PATH``) and does a lightweight HTTP ``/healthz`` probe
against ``SEARXNG_URL`` to distinguish "container up" from "actually
answering queries" — mirrors the ``src/hyperv_client.py`` / ``ups_client``
"shell out, flatten, partial data stays 200" shape, including the
shared ``NO_WINDOW`` guard (``src/_no_window.py``) so no console window pops on
each Home-view poll.

Status reads never raise — a missing container, a stopped one, or an
unreachable ``/healthz`` all come back as an ``available=False``
:class:`SearxngState` with a distinct ``error``. ``start_searxng`` raises
:class:`SearxngConfigError` / :class:`SearxngCommandError` instead, so the
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
from typing import Any, Dict, Optional

from dotenv import load_dotenv

from src._no_window import NO_WINDOW

logger = logging.getLogger("searxng")

load_dotenv()

_CONTAINER_NAME = "searxng"
_COMPOSE_PATH_ENV = "SEARXNG_COMPOSE_PATH"
_URL_ENV = "SEARXNG_URL"
_DEFAULT_URL = "http://192.168.0.13:8085"


class SearxngConfigError(RuntimeError):
    """``SEARXNG_COMPOSE_PATH`` is unset/empty — the core can't know which stack to start."""


class SearxngCommandError(RuntimeError):
    """``docker compose up -d`` failed for some reason."""


@dataclass(frozen=True)
class SearxngState:
    """Flattened SearXNG container status for the Home Assistant card."""

    available: bool
    container_status: Optional[str] = None
    reachable: bool = False
    url: Optional[str] = None
    error: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


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
    """Read the container's live status. Never raises — degrades to ``available=False``."""
    url = searxng_url()
    status = _container_status()
    reachable = _probe_healthz(url) if status == "running" else False
    available = status == "running" and reachable

    error = None
    if status == "not_found":
        error = "SearXNG container not found — start it from this card or `start_searxng.bat`."
    elif status != "running":
        error = f"SearXNG container is {status}."
    elif not reachable:
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
    either: up but wedged, not answering ``/healthz`` (issue #716). Only the
    watchdog calls this, and only after the unreachable state has persisted
    past its grace window — a freshly-booting container must never be
    recreated out from under itself.
    """
    return _compose(["restart"], "docker compose restart failed", "restart")


def _compose(args: list, fallback_err: str, label: str) -> SearxngState:
    """Run one ``docker compose`` subcommand against the stack, then read state back."""
    path = compose_path()  # SearxngConfigError propagates → router maps to 503
    result = subprocess.run(
        ["docker", "compose", "-f", path, *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=NO_WINDOW,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or fallback_err).strip()
        logger.warning("⚠️  SearXNG %s failed: %s", label, err)
        raise SearxngCommandError(_short(err))
    return fetch_searxng_state()


def _container_status() -> str:
    result = subprocess.run(
        ["docker", "inspect", _CONTAINER_NAME, "--format", "{{.State.Status}}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        creationflags=NO_WINDOW,
    )
    if result.returncode != 0:
        return "not_found"
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
