"""
iCloud Find My presence client
==============================
Read-only client for Apple Find My device locations, plus the attended
browser-trust renewal flow the PWA drives. It is load-bearing for automation,
not a spike: ``fetch_presence()`` produces the ``PresenceCorroboration`` signal
``src/presence_engine.py`` uses to let a *stale* webhook person still count as
fresh (issue #653), which is what permits an automatic arm or disarm of the
house alarm. It drives no HVAC action.

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

Health model (issue #658): a session is healthy when the Find My fetch
succeeds — never because of pyicloud's in-memory ``requires_2fa`` flag.
pyicloud's Find My sub-service re-authenticates on its own when Apple answers
450, and when the browser-trust token isn't honoured that internal re-auth
leaves ``requires_2fa`` true even though the very same fetch then serves every
device. Gating on the flag marked perfectly working sessions broken.

Browser-trust renewal (issue #659): Apple's ~30-day browser trust makes a fresh
session build a silent token login; once it lapses every fresh build costs a
password (SRP) sign-in Apple throttles. Renewing it is a two-step, attended
flow that must run inside ONE live ``PyiCloudService`` — the 2FA push and the
code entry belong to the same Apple auth session — so
:func:`begin_trust_renewal` parks the challenged service in
:data:`_PENDING_TRUST` and :func:`complete_trust_renewal` finishes it, then
swaps the trusted service into :data:`_SERVICE_CACHE` for the tray's next poll.
"""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass, replace
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
    friendly_name: str = ""  # e.g. "Roberto"/"Ana" (issue #655); "" -> caller falls back to "account {label}"
    # Whether pyicloud may ask Apple to push a 2FA code to the trusted devices
    # when a fresh sign-in ends up needing one (issue #658). True for the
    # attended CLI (someone is there to read the code); the unattended tray
    # refresher sets False — nobody in that process will ever type the code,
    # so the push is pure noise on every phone in the household.
    request_2fa_push: bool = True

    @property
    def display_name(self) -> str:
        """Name this account for a human — friendly name if set, else the Apple
        ID email (issue #657: a bare "account 1"/"account 2" gave no way to tell
        which real account a message or row was about)."""

        return self.friendly_name or self.email


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
            friendly_name=(os.getenv("ICLOUD_LABEL") or "").strip(),
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
                friendly_name=(os.getenv("ICLOUD_LABEL_2") or "").strip(),
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

    ``verification_code`` is only needed to (re-)trust the session when Apple
    asks for 2FA; it is applied both before and after the fetch (see below).
    Without a code the fetch is still attempted — Find My serves an untrusted
    session — and only a fetch that Apple actually refuses raises
    :class:`PresenceAuthError` (with a CLI-friendly instruction rather than
    blocking for input inside library code).
    """

    cfg = config or load_presence_config()
    cfg.session_dir.mkdir(parents=True, exist_ok=True)
    api = _connect(cfg)

    # Issue #658: the fetch itself is the health check. ``requires_2fa`` is
    # consulted only to *apply* an explicitly supplied code (the CLI's
    # ``--2fa-code``), never to refuse a fetch — pyicloud's Find My
    # sub-service flips that flag on its own internal re-auth while still
    # serving every device, so a pre-check turned healthy sessions into
    # ``2fa_required`` on the very next poll. Auth failures raised *by* the
    # fetch are what mean "really broken" (mapped to PresenceAuthError below).
    #
    # Neither failure path evicts the cached session (issue #656): eviction is
    # a full Apple sign-in handshake, and only the presence refresher's
    # backoff-gated self-heal (``invalidate_session``) may decide to pay it.
    if verification_code:
        _complete_2fa(api, verification_code=verification_code, trust_session=trust_session)

    home = location if location is not None else load_location_config()
    try:
        devices = _iter_devices(api.devices)
    except Exception as exc:  # noqa: BLE001 - re-raised or mapped, never swallowed
        if _is_auth_failure(exc):
            raise PresenceAuthError(
                f"iCloud Find My refused the session ({type(exc).__name__}: {exc}). "
                "Re-trust it from the app (Presence card → Renew trust) or via "
                "src.list_presence --account <N> --renew-trust."
            ) from exc
        raise
    entities = [
        _entity_from_device(device, home, home_radius_m=cfg.home_radius_m)
        for device in devices
    ]

    if verification_code:
        # The internal re-auth that needs the code happens *inside* the fetch
        # (Find My's 450 → accountLogin → SRP), so a code passed up-front only
        # renews the browser trust if it is applied again afterwards.
        _complete_2fa(api, verification_code=verification_code, trust_session=trust_session)

    _warn_once_if_untrusted(api, cfg)
    logger.info("✅ Fetched %d iCloud Find My entit(y/ies)", len(entities))
    return entities


# Session dirs already warned about serving on an untrusted session, so the
# warning is edge-triggered per session build rather than repeated every poll.
_UNTRUSTED_WARNED: set[str] = set()


def _warn_once_if_untrusted(api: Any, config: PresenceConfig) -> None:
    """One WARNING per session build when Find My serves without browser trust.

    Not a failure — the data is flowing — but each *fresh* build of such a
    session costs a full password sign-in with Apple (and, on the attended
    CLI, a 2FA push), so the owner should re-trust it at some point.
    """

    key = str(config.session_dir)
    if not bool(getattr(api, "requires_2fa", False)) or key in _UNTRUSTED_WARNED:
        return
    _UNTRUSTED_WARNED.add(key)
    logger.warning(
        "⚠️ iCloud account %s: Find My is serving on an untrusted session "
        "(browser trust expired). Locations keep flowing; each fresh sign-in "
        "costs a password login until re-trusted from the app (Presence card → "
        "Renew trust) or via src.list_presence --account %s --renew-trust.",
        config.label,
        config.label,
    )


def _is_auth_failure(exc: BaseException) -> bool:
    """Whether a fetch exception means the session itself needs re-auth.

    Lazy import: tests substitute a fake service and must not need pyicloud
    at import time; without pyicloud nothing can be an auth failure anyway.
    """

    try:
        from pyicloud.exceptions import (
            PyiCloud2FARequiredException,
            PyiCloud2SARequiredException,
            PyiCloudAuthRequiredException,
            PyiCloudFailedLoginException,
        )
    except ImportError:  # pragma: no cover - covered by requirements
        return False
    return isinstance(
        exc,
        (
            PyiCloud2FARequiredException,
            PyiCloud2SARequiredException,
            PyiCloudAuthRequiredException,
            PyiCloudFailedLoginException,
        ),
    )


# Authenticated pyicloud sessions, keyed by session dir (one per account).
# Reused across polls (issue #651): rebuilding a ``PyiCloudService`` from
# scratch performs a full Apple sign-in handshake, and doing that every
# ``PRESENCE_ICLOUD_REFRESH_INTERVAL_S`` poll (every ~15 min, forever) reads
# to Apple as a fresh sign-in, which was triggering repeated "someone is
# trying to access your account" prompts on the user's trusted devices.
_SERVICE_CACHE: dict[str, Any] = {}


def _build_service(config: PresenceConfig) -> Any:
    """Perform the real Apple sign-in handshake for one account.

    Split out from :func:`_connect` purely so tests can substitute a fake
    service without an import-time dependency on the real ``pyicloud`` package.
    """

    service_cls = _service_class(request_2fa_push=config.request_2fa_push)
    logger.info("ℹ️ Authenticating with iCloud")
    return service_cls(
        config.email,
        config.password,
        cookie_directory=str(config.session_dir),
        with_family=config.with_family,
    )


def _service_class(*, request_2fa_push: bool) -> Any:
    """Return the ``PyiCloudService`` class to build sessions with.

    ``request_2fa_push=False`` (issue #658) returns a subclass whose
    ``_request_2fa_code`` hook is a no-op: pyicloud calls that hook right after
    an SRP password login that Apple answers with "2FA required", and it asks
    Apple to push a code to every trusted device (plus an SMS attempt). In an
    unattended process nobody ever enters that code — the session serves Find
    My regardless — so the push is only noise. The hook is private to the
    pinned ``pyicloud==2.6.5``; :func:`_assert_push_hook_present` (and its
    test) makes an upgrade that renames it fail loud rather than silently
    start pushing again.
    """

    try:
        from pyicloud import PyiCloudService
    except ImportError as exc:  # pragma: no cover - covered by requirements
        raise PresenceConfigError(
            "pyicloud is not installed. Run pip install -r requirements.txt."
        ) from exc

    if request_2fa_push:
        return PyiCloudService

    _assert_push_hook_present(PyiCloudService)

    class _QuietPyiCloudService(PyiCloudService):  # type: ignore[misc,valid-type]
        """pyicloud session that never asks Apple to push a 2FA code."""

        def _request_2fa_code(self) -> None:
            logger.info(
                "ℹ️ iCloud sign-in needs 2FA; not requesting Apple's trusted-device "
                "push (unattended session, nobody here can enter the code)"
            )

    return _QuietPyiCloudService


def _assert_push_hook_present(service_cls: Any) -> None:
    """Fail loud if pyicloud no longer exposes the hook the quiet subclass overrides."""

    if not callable(getattr(service_cls, "_request_2fa_code", None)):
        raise PresenceConfigError(
            "pyicloud no longer defines PyiCloudService._request_2fa_code; the "
            "unattended no-push override in src.presence_client must be re-pinned "
            "to the new hook before upgrading (issue #658)."
        )


def _connect(config: PresenceConfig) -> Any:
    """Return this account's cached authenticated session, building one if needed."""

    cache_key = str(config.session_dir)
    cached = _SERVICE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    api = _build_service(config)
    _SERVICE_CACHE[cache_key] = api
    return api


def invalidate_session(config: PresenceConfig) -> None:
    """Evict a cached session so the next :func:`_connect` rebuilds it from scratch.

    Public (issue #655): the presence refresher calls this to force a fresh
    Apple sign-in handshake when retrying a session stuck in a failed state —
    see :mod:`app.webapp.presence_refresher`'s backoff-gated retry.
    """

    _SERVICE_CACHE.pop(str(config.session_dir), None)
    _UNTRUSTED_WARNED.discard(str(config.session_dir))


def session_trust_state(config: PresenceConfig) -> Optional[bool]:
    """Whether this account's *cached* session currently holds browser trust.

    ``None`` when no session is cached yet (nothing to say). Reads pyicloud's
    ``requires_2fa`` — the same flag :func:`_warn_once_if_untrusted` keys off —
    so the diagnostics row and the log WARNING can never disagree. Cheap: no
    Apple round-trip.
    """

    api = _SERVICE_CACHE.get(str(config.session_dir))
    if api is None:
        return None
    return not bool(getattr(api, "requires_2fa", False))


# ------------------------------------------------------- browser-trust renewal
# (issue #659) One challenged-but-not-yet-verified PyiCloudService per session
# dir, parked between begin_trust_renewal() and complete_trust_renewal(). The
# 2FA code Apple pushes is only valid for the auth session (scnt /
# X-Apple-ID-Session-Id) that requested it, which lives on this exact object.
PENDING_TRUST_TTL_S = 10 * 60
MAX_CODE_ATTEMPTS = 2  # rejected codes tolerated per push before a new begin is needed


@dataclass(frozen=True)
class TrustRenewalState:
    """Outcome of one step of the attended browser-trust renewal (issue #659).

    ``status`` after :func:`begin_trust_renewal`: ``code_sent`` /
    ``already_trusted`` / ``failed``; after :func:`complete_trust_renewal`:
    ``trusted`` / ``invalid_code`` / ``expired`` / ``failed``. ``detail`` is a
    human-readable line for the UI/CLI (Apple's own message on a failure).
    ``trusted`` is what the account's session holds after this step, when known.
    """

    status: str
    detail: str = ""
    trusted: Optional[bool] = None


@dataclass
class _PendingTrust:
    api: Any
    started_at: float  # time.monotonic()
    attempts: int = 0


_PENDING_TRUST: dict[str, _PendingTrust] = {}


def begin_trust_renewal(config: PresenceConfig) -> TrustRenewalState:
    """Ask Apple to challenge a fresh sign-in so a 2FA code reaches the owner.

    Builds a **fresh** service (with ``request_2fa_push=True`` — this is the
    attended flow, the push is wanted) and forces the full challenge with
    ``authenticate(force_refresh=True)``: token login → Apple says untrusted →
    SRP password login → 2FA required → pyicloud asks Apple to push a code to
    the trusted devices. The challenged service is parked in
    :data:`_PENDING_TRUST` for :func:`complete_trust_renewal`; any previous
    pending renewal for this account is discarded. Never prompts.
    """

    key = str(config.session_dir)
    _discard_pending(key)
    config.session_dir.mkdir(parents=True, exist_ok=True)
    try:
        api = _build_service(replace(config, request_2fa_push=True))
        # A brand-new session dir has no token to log in with, so the build
        # itself already ran SRP and pushed the code — forcing a second
        # sign-in here would push twice. Only a token-authenticated build
        # needs the challenge forced.
        if not bool(getattr(api, "requires_2fa", False)):
            api.authenticate(force_refresh=True)
    except Exception as exc:  # noqa: BLE001 - mapped to a user-facing state
        logger.warning(
            "⚠️ iCloud account %s: trust renewal could not start (%s: %s)",
            config.label,
            type(exc).__name__,
            exc,
        )
        return TrustRenewalState("failed", detail=_apple_detail(exc))

    if bool(getattr(api, "requires_2fa", False)):
        _PENDING_TRUST[key] = _PendingTrust(api=api, started_at=time.monotonic())
        method = str(getattr(api, "two_factor_delivery_method", "unknown") or "unknown")
        detail = (
            "Apple sent a 6-digit code by SMS to the account's trusted phone number."
            if method == "sms"
            else "Apple pushed a 6-digit code to the account's trusted devices."
        )
        logger.info(
            "📲 iCloud account %s: 2FA code requested for trust renewal (delivery: %s)",
            config.label,
            method,
        )
        return TrustRenewalState("code_sent", detail=detail, trusted=False)

    if bool(getattr(api, "is_trusted_session", False)):
        # Apple honoured the trust token after all — adopt this fresh, trusted
        # service so the tray stops carrying an untrusted one for the account.
        _adopt_service(config, api)
        logger.info("ℹ️ iCloud account %s: session already trusted, nothing to renew", config.label)
        return TrustRenewalState(
            "already_trusted",
            detail="Apple still trusts this session; nothing to renew.",
            trusted=True,
        )

    logger.warning(
        "⚠️ iCloud account %s: Apple issued no 2FA challenge and reports no trust",
        config.label,
    )
    return TrustRenewalState(
        "failed",
        detail="Apple issued no 2FA challenge and did not report the session as trusted.",
        trusted=False,
    )


def complete_trust_renewal(config: PresenceConfig, code: str) -> TrustRenewalState:
    """Verify the pushed code on the pending service and prove the new trust.

    ``validate_2fa_code`` in the pinned ``pyicloud==2.6.5`` already calls
    ``trust_session()`` (GET ``2sv/trust`` → new trust token) and then
    re-authenticates with ``accountLogin`` — so a ``True`` return means Apple
    both accepted the code *and* reported ``hsaTrustedBrowser`` on a fresh
    login; ``is_trusted_session`` afterwards reads that fresh response, not the
    stale pre-challenge one. On success the trusted service replaces the
    account's cached one (the tray's next poll reuses it) and the untrusted
    WARNING latch is cleared. A rejected code keeps the pending challenge for
    one more attempt, then a new :func:`begin_trust_renewal` is required.
    The code is never logged.
    """

    key = str(config.session_dir)
    pending = _PENDING_TRUST.get(key)
    if pending is None:
        return TrustRenewalState(
            "expired", detail="No trust renewal in progress for this account — start again."
        )
    if time.monotonic() - pending.started_at > PENDING_TRUST_TTL_S:
        _discard_pending(key)
        logger.info("ℹ️ iCloud account %s: pending trust renewal expired", config.label)
        return TrustRenewalState(
            "expired", detail="The code request expired — start again to get a new code."
        )

    api = pending.api
    logger.info(
        "🔎 iCloud account %s: verifying 2FA code via %s (delivery: %s)",
        config.label,
        _verification_path(api),
        str(getattr(api, "two_factor_delivery_method", "unknown") or "unknown"),
    )
    try:
        accepted = bool(api.validate_2fa_code(code))
    except Exception as exc:  # noqa: BLE001 - mapped to a user-facing state
        _discard_pending(key)
        logger.warning(
            "⚠️ iCloud account %s: code verification failed (%s: %s)",
            config.label,
            type(exc).__name__,
            exc,
        )
        return TrustRenewalState("failed", detail=_apple_detail(exc), trusted=False)

    if not accepted and _apple_granted_trust_anyway(api):
        # Issue #662: pyicloud's trusted-device *bridge* verification can
        # report failure client-side after Apple has already accepted the
        # approval (seen live 2026-08-19: three "rejected" codes, then the
        # next begin found the session already trusted). trust_session() is
        # a push-free, SRP-free re-login by token, so it is a safe second
        # opinion before telling the user the code was wrong.
        accepted = True
        logger.info(
            "ℹ️ iCloud account %s: code reported rejected client-side but Apple "
            "granted browser trust — treating as verified",
            config.label,
        )

    if not accepted:
        pending.attempts += 1
        if pending.attempts >= MAX_CODE_ATTEMPTS:
            _discard_pending(key)
            detail = "Apple rejected the code again — start again to get a new code."
        else:
            detail = "Apple rejected the code — check it and try once more."
        logger.warning(
            "⚠️ iCloud account %s: Apple rejected the 2FA code (attempt %d/%d)",
            config.label,
            pending.attempts,
            MAX_CODE_ATTEMPTS,
        )
        return TrustRenewalState("invalid_code", detail=detail, trusted=False)

    _discard_pending(key)
    trusted = bool(getattr(api, "is_trusted_session", False)) and not bool(
        getattr(api, "requires_2fa", False)
    )
    if not trusted:
        logger.warning(
            "⚠️ iCloud account %s: code accepted but Apple did not grant browser trust",
            config.label,
        )
        return TrustRenewalState(
            "failed",
            detail="Apple accepted the code but did not grant browser trust — start again.",
            trusted=False,
        )

    _adopt_service(config, api)
    logger.info("✅ iCloud account %s: browser trust renewed", config.label)
    return TrustRenewalState(
        "trusted",
        detail="Browser trust renewed; fresh sign-ins are silent token logins again.",
        trusted=True,
    )


def _discard_pending(key: str) -> None:
    _PENDING_TRUST.pop(key, None)


def _verification_path(api: Any) -> str:
    """Name the pyicloud 2.6.5 path ``validate_2fa_code`` will take (log breadcrumb)."""

    bridge_state = getattr(api, "_trusted_device_bridge_state", None)
    if bridge_state is None:
        return "legacy endpoint"
    if bool(getattr(bridge_state, "uses_legacy_trusted_device_verifier", False)):
        return "legacy endpoint (bridge opted out)"
    return "trusted-device bridge"


def _apple_granted_trust_anyway(api: Any) -> bool:
    """After a client-side ``False`` from ``validate_2fa_code``, ask Apple.

    ``trust_session()`` (public pyicloud API) GETs ``2sv/trust`` and re-logs
    in by token — no SRP, no push. It returns True only when Apple reports the
    session trusted; a False leaves ``is_trusted_session`` false (so
    ``requires_2fa`` stays true and the pending challenge is still retryable).
    """

    trust = getattr(api, "trust_session", None)
    if not callable(trust):
        return False
    try:
        granted = bool(trust())
    except Exception as exc:  # noqa: BLE001 - a failed probe is just "not trusted"
        logger.info("ℹ️ trust probe after rejected code failed: %s: %s", type(exc).__name__, exc)
        return False
    return (
        granted
        and bool(getattr(api, "is_trusted_session", False))
        and not bool(getattr(api, "requires_2fa", False))
    )


def _adopt_service(config: PresenceConfig, api: Any) -> None:
    """Make ``api`` the account's cached session, retiring the previous one.

    The retired service's Find My manager runs a background refresh thread on
    the *same* on-disk session file; left alive it would keep re-saving the
    old, untrusted session data over the freshly written trust token, so it is
    stopped first (a no-op when Find My was never initialised on it).
    """

    key = str(config.session_dir)
    old = _SERVICE_CACHE.get(key)
    if old is not None and old is not api:
        _stop_background_refresh(old)
    _quiet_adopted_service(api)
    _SERVICE_CACHE[key] = api
    _UNTRUSTED_WARNED.discard(key)


def _quiet_adopted_service(api: Any) -> None:
    """Demote a service built for the attended flow to the no-push class.

    The renewal builds its service with ``request_2fa_push=True`` because the
    push is wanted *then*. Once adopted it lives in the unattended tray for
    the rest of the process — if browser trust lapses again (~30 days) with
    no restart in between, pyicloud's internal Find My re-auth on this very
    object would otherwise push a code from the tray, the exact spam #658
    removed. Only real pyicloud services are re-classed; test doubles are
    left alone.
    """

    try:
        from pyicloud import PyiCloudService
    except ImportError:  # pragma: no cover - covered by requirements
        return
    if not isinstance(api, PyiCloudService):
        return
    quiet = _service_class(request_2fa_push=False)
    if type(api) is quiet:
        return
    try:
        api.__class__ = quiet
    except TypeError as exc:  # pragma: no cover - layout mismatch, never seen
        logger.warning("⚠️ Could not demote adopted iCloud service to no-push class: %s", exc)


def _stop_background_refresh(api: Any) -> None:
    # ``_devices`` is pyicloud's lazily-built FindMyiPhoneServiceManager (None
    # until ``.devices`` is first read); its ``stop_event`` ends the monitor
    # thread. Identity checks only — ``bool(manager)`` calls ``__len__``, which
    # would trigger a network refresh.
    manager = getattr(api, "_devices", None)
    stop = getattr(manager, "stop_event", None) if manager is not None else None
    if stop is not None and callable(getattr(stop, "set", None)):
        stop.set()


def _apple_detail(exc: BaseException) -> str:
    """Apple's own message for a failed step, or a neutral fallback."""

    text = str(exc).strip()
    return text or "Apple did not accept the sign-in; try again in a few minutes."


def _complete_2fa(
    api: Any, *, verification_code: Optional[str], trust_session: bool
) -> None:
    """Validate a supplied 2FA code if Apple currently requires one.

    Only reached with an explicit ``verification_code`` (issue #658) — the
    absence of a code is never a reason to refuse a fetch, so the "no code
    given" branch below is only hit by direct callers/tests.
    """

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
