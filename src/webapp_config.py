"""Webapp configuration + secrets loader.

Holds the FastAPI webapp's bind host/port and the optional auth gate
(`auth_token` / `auth_password`). Authored by the helper scripts
(`scripts/gen_token.py`, `scripts/set_password.py`) and read by the
server at boot. Kept out of `melcloud_client.py` because that module is
the UI-free MELCloud core and must not grow webapp concerns.

The real `config/webapp_config.json` is gitignored (it carries
secrets); `config/webapp_config.sample.json` is committed as the
template. A missing file is not an error — first run uses the defaults.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urlparse, urlunparse

logger = logging.getLogger("melcloud.webapp_config")

DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "webapp_config.json"
)

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8447


@dataclass
class WebappConfig:
    """User-authored, persisted webapp settings."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    # Bearer token enforced when the request did NOT come from a loopback
    # IP. Empty string disables enforcement entirely.
    auth_token: str = ""
    # Optional password gate that hands the bearer token back to the
    # browser when typed correctly — lets a fresh device (e.g. an iOS
    # PWA whose storage is partitioned from Safari) bootstrap without a
    # tokenised URL.
    auth_password: str = ""


def load_webapp_config(path: Optional[Path] = None) -> WebappConfig:
    """Load the webapp config, falling back to defaults if the file is missing."""
    target = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not target.exists():
        logger.info(
            "📂 webapp_config not found at %s, using defaults", target
        )
        return WebappConfig()

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "⚠️ Could not read %s (%s); falling back to defaults", target, exc
        )
        return WebappConfig()

    cfg = WebappConfig(
        host=str(raw.get("host", DEFAULT_HOST)),
        port=int(raw.get("port", DEFAULT_PORT)),
        auth_token=str(raw.get("auth_token", "")),
        auth_password=str(raw.get("auth_password", "")),
    )
    _validate(cfg)
    return cfg


def save_webapp_config(cfg: WebappConfig, path: Optional[Path] = None) -> Path:
    """Atomically write the config back to disk."""
    target = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "host": cfg.host,
        "port": cfg.port,
        "auth_token": cfg.auth_token,
        "auth_password": cfg.auth_password,
    }

    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, target)
    logger.info("💾 Saved webapp_config to %s", target)
    return target


def update_webapp_config(**fields) -> WebappConfig:
    """Read, patch, save — convenience for the helper scripts."""
    current = load_webapp_config()
    patched = replace(current, **fields)
    _validate(patched)
    save_webapp_config(patched)
    return patched


def append_auth_token(url: str, token: Optional[str]) -> str:
    """Return ``url`` with ``?token=<token>`` appended when ``token`` is set."""
    if not token:
        return url
    parsed = urlparse(url)
    extra = urlencode({"token": token})
    new_query = f"{parsed.query}&{extra}" if parsed.query else extra
    return urlunparse(parsed._replace(query=new_query))


def _validate(cfg: WebappConfig) -> None:
    if not (1 <= cfg.port <= 65535):
        raise ValueError(f"port out of range: {cfg.port}")
