"""
iCloud Find My presence client
==============================
Read-only spike client for Apple Find My device locations. The goal is to
prove whether iCloud can provide a useful home/away input for later HVAC
automation; this module does not drive any HVAC action.

Config (from ``.env``):

* ``ICLOUD_EMAIL`` / ``ICLOUD_PASSWORD`` - Apple Account credentials.
* ``ICLOUD_SESSION_DIR`` - optional cookie/session cache directory. Defaults to
  ``webapp/icloud_session`` and must remain gitignored because it contains live
  Apple session material.
* ``ICLOUD_EMAIL_2`` / ``ICLOUD_PASSWORD_2`` / ``ICLOUD_SESSION_DIR_2`` -
  optional **second** Apple Account (issue #478). Family Sharing only exposes
  one account's own devices plus family members sharing location with *that*
  account's group, so a phone on a different Apple ID never appears in the
  first account's read. Configuring a second account authenticates it the same
  way and merges its Find My devices into the same snapshot. Its session dir
  defaults to ``webapp/icloud_session_2`` (also gitignored). Both vars must be
  set for the second account to be used; leaving them blank keeps the
  single-account setup unchanged.
* ``PRESENCE_HOME_RADIUS_M`` - optional home radius used to derive home/away
  (shared by every account).

``pyicloud`` may require an interactive 2FA code the first time a session is
created, and again when Apple expires the trusted session. 2FA is per Apple ID,
so each configured account trips it — and is trusted — independently.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from dotenv import load_dotenv

from src.location_config import LocationConfig, load_location_config

logger = logging.getLogger("presence")

DEFAULT_SESSION_DIR = (
    Path(__file__).resolve().parent.parent / "webapp" / "icloud_session"
)
DEFAULT_SESSION_DIR_2 = (
    Path(__file__).resolve().parent.parent / "webapp" / "icloud_session_2"
)
DEFAULT_HOME_RADIUS_M = 200.0


class PresenceConfigError(RuntimeError):
    """Raised when iCloud presence credentials are missing or invalid."""


class PresenceAuthError(RuntimeError):
    """Raised when iCloud needs an interactive auth step before reads work."""


@dataclass(frozen=True)
class PresenceConfig:
    """Runtime iCloud presence config for a single Apple Account, from ``.env``."""

    email: str
    password: str
    session_dir: Path = DEFAULT_SESSION_DIR
    home_radius_m: float = DEFAULT_HOME_RADIUS_M
    with_family: bool = True
    label: str = "1"  # 1-based account index, for per-account diagnostics/CLI


@dataclass(frozen=True)
class PresenceEntity:
    """Flattened read-only Find My entity."""

    entity_id: str
    name: str
    model: Optional[str]
    device_class: Optional[str]
    latitude: Optional[float]
    longitude: Optional[float]
    horizontal_accuracy_m: Optional[float]
    last_seen: Optional[datetime]
    battery_level_pct: Optional[int]
    battery_status: Optional[str]
    distance_from_home_m: Optional[float] = None
    at_home: Optional[bool] = None

    @property
    def has_location(self) -> bool:
        """Whether this entity currently has usable coordinates."""

        return self.latitude is not None and self.longitude is not None


def _session_dir_from_env(name: str, default: Path) -> Path:
    """Resolve a session/cookie directory from ``.env``, falling back to a default."""

    configured = (os.getenv(name) or "").strip()
    return Path(configured) if configured else default


def load_presence_configs(
    primary_session_dir: Optional[Path] = None,
) -> list[PresenceConfig]:
    """Read every configured iCloud account from ``.env`` (issue #478).

    Account 1 (``ICLOUD_EMAIL`` / ``ICLOUD_PASSWORD``) is required and always
    first. Account 2 (``ICLOUD_EMAIL_2`` / ``ICLOUD_PASSWORD_2``) is optional and
    only included when both its email and password are set; a partially-set
    second account is skipped with a warning rather than failing the read.
    ``primary_session_dir`` overrides account 1's session directory (used by the
    CLI/tests) and never affects account 2.
    """

    load_dotenv(override=True)
    home_radius_m = _env_float("PRESENCE_HOME_RADIUS_M", DEFAULT_HOME_RADIUS_M)

    email = (os.getenv("ICLOUD_EMAIL") or "").strip()
    password = (os.getenv("ICLOUD_PASSWORD") or "").strip()
    if not email or not password:
        raise PresenceConfigError(
            "Missing iCloud credentials. Set ICLOUD_EMAIL and ICLOUD_PASSWORD "
            "in .env before running src.list_presence."
        )
    primary_dir = (
        primary_session_dir
        if primary_session_dir is not None
        else _session_dir_from_env("ICLOUD_SESSION_DIR", DEFAULT_SESSION_DIR)
    )
    configs = [
        PresenceConfig(
            email=email,
            password=password,
            session_dir=primary_dir,
            home_radius_m=home_radius_m,
            label="1",
        )
    ]

    email2 = (os.getenv("ICLOUD_EMAIL_2") or "").strip()
    password2 = (os.getenv("ICLOUD_PASSWORD_2") or "").strip()
    if email2 and password2:
        configs.append(
            PresenceConfig(
                email=email2,
                password=password2,
                session_dir=_session_dir_from_env(
                    "ICLOUD_SESSION_DIR_2", DEFAULT_SESSION_DIR_2
                ),
                home_radius_m=home_radius_m,
                label="2",
            )
        )
    elif email2 or password2:
        logger.warning(
            "⚠️ ICLOUD_EMAIL_2/ICLOUD_PASSWORD_2 only partially set; "
            "second iCloud account skipped."
        )

    return configs


def load_presence_config(session_dir: Optional[Path] = None) -> PresenceConfig:
    """Read the primary iCloud account settings from ``.env``.

    Retained for callers that only need the first account (and the single-account
    default of :func:`fetch_presence`); the multi-account read is
    :func:`load_presence_configs`.
    """

    return load_presence_configs(primary_session_dir=session_dir)[0]


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters between two WGS84 coordinates."""

    radius_m = 6_371_000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return radius_m * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def fetch_presence(
    *,
    verification_code: Optional[str] = None,
    trust_session: bool = True,
    location: Optional[LocationConfig] = None,
    config: Optional[PresenceConfig] = None,
) -> list[PresenceEntity]:
    """Fetch Find My entities from iCloud and return normalized snapshots.

    ``verification_code`` is only needed when Apple asks for 2FA. If omitted in
    that state, the function raises :class:`PresenceAuthError` with a CLI-friendly
    instruction rather than blocking for input inside library code.
    """

    cfg = config or load_presence_config()
    cfg.session_dir.mkdir(parents=True, exist_ok=True)
    api = _connect(cfg)
    _complete_2fa(api, verification_code=verification_code, trust_session=trust_session)

    home = location if location is not None else load_location_config()
    devices = _iter_devices(api.devices)
    entities = [
        _entity_from_device(device, home, home_radius_m=cfg.home_radius_m)
        for device in devices
    ]
    logger.info("✅ Fetched %d iCloud Find My entit(y/ies)", len(entities))
    return entities


def _connect(config: PresenceConfig) -> Any:
    """Create a ``pyicloud`` service instance lazily so tests can avoid import-time I/O."""

    try:
        from pyicloud import PyiCloudService
    except ImportError as exc:  # pragma: no cover - covered by requirements
        raise PresenceConfigError(
            "pyicloud is not installed. Run pip install -r requirements.txt."
        ) from exc

    logger.info("ℹ️ Authenticating with iCloud")
    return PyiCloudService(
        config.email,
        config.password,
        cookie_directory=str(config.session_dir),
        with_family=config.with_family,
    )


def _complete_2fa(
    api: Any, *, verification_code: Optional[str], trust_session: bool
) -> None:
    """Validate a 2FA code if Apple requires one."""

    if not bool(getattr(api, "requires_2fa", False)):
        return

    if not verification_code:
        raise PresenceAuthError(
            "iCloud requires 2FA. Re-run with --2fa-code <code> from a trusted "
            "Apple device; the trusted session is cached under ICLOUD_SESSION_DIR."
        )

    if not api.validate_2fa_code(verification_code):
        raise PresenceAuthError("iCloud rejected the supplied 2FA code.")

    if trust_session and hasattr(api, "trust_session"):
        api.trust_session()

    if bool(getattr(api, "requires_2fa", False)):
        raise PresenceAuthError("iCloud still requires 2FA after code validation.")


def _iter_devices(devices: Any) -> Iterable[Any]:
    """Return a stable iterable for the pyicloud device manager."""

    if hasattr(devices, "refresh"):
        devices.refresh(locate=True)
    return list(devices)


def _entity_from_device(
    device: Any,
    home: Optional[LocationConfig] = None,
    *,
    home_radius_m: float = DEFAULT_HOME_RADIUS_M,
) -> PresenceEntity:
    """Normalize a pyicloud device object or device-like test double."""

    data = _device_data(device)
    location = _coerce_mapping(_device_value(device, data, "location"))
    lat = _as_float(location.get("latitude") if location else None)
    lon = _as_float(location.get("longitude") if location else None)
    last_seen = _as_datetime(location.get("timeStamp") if location else None)
    distance = (
        distance_m(home.lat, home.lon, lat, lon)
        if home is not None and lat is not None and lon is not None
        else None
    )
    battery_level = _battery_pct(_device_value(device, data, "batteryLevel"))
    battery_status = _as_str(_device_value(device, data, "batteryStatus"))
    if battery_level == 0 and (battery_status is None or battery_status == "Unknown"):
        battery_level = None

    return PresenceEntity(
        entity_id=str(_device_value(device, data, "id") or ""),
        name=str(_device_value(device, data, "name") or "Unknown"),
        model=_as_str(_device_value(device, data, "deviceDisplayName", "deviceModel")),
        device_class=_as_str(_device_value(device, data, "deviceClass")),
        latitude=lat,
        longitude=lon,
        horizontal_accuracy_m=_as_float(location.get("horizontalAccuracy") if location else None),
        last_seen=last_seen,
        battery_level_pct=battery_level,
        battery_status=battery_status,
        distance_from_home_m=distance,
        at_home=distance <= home_radius_m if distance is not None else None,
    )


def _device_data(device: Any) -> dict[str, Any]:
    data = getattr(device, "data", None)
    return data if isinstance(data, dict) else {}


def _device_value(device: Any, data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data:
            return data[key]
        try:
            return getattr(device, key)
        except AttributeError:
            continue
    return None


def _coerce_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _battery_pct(value: Any) -> Optional[int]:
    level = _as_float(value)
    if level is None:
        return None
    if 0 <= level <= 1:
        level *= 100
    return max(0, min(100, round(level)))


def _as_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if timestamp > 10_000_000_000:
        timestamp /= 1000
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("⚠️ Invalid %s=%s; using %.0f", name, raw, default)
        return default
