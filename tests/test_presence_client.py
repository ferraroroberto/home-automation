"""Unit tests for :mod:`src.presence_client`.

Pure normalization and auth-control tests: no Apple network calls, no real
credentials, and no committed home coordinates.
"""

from __future__ import annotations

from datetime import timezone

import pytest

from src import presence_client as P
from src.location_config import LocationConfig


class _FakeDevices:
    def __init__(self, devices: list[object]) -> None:
        self._devices = devices
        self.refreshed = False

    def refresh(self, locate: bool = True) -> None:
        self.refreshed = locate

    def __iter__(self):
        return iter(self._devices)


class _FakeApi:
    def __init__(self, *, requires_2fa: bool = False, validates: bool = True) -> None:
        self.requires_2fa = requires_2fa
        self.validates = validates
        self.trusted = False
        self.devices = _FakeDevices([])

    def validate_2fa_code(self, code: str) -> bool:
        self.requires_2fa = False
        return self.validates and code == "123456"

    def trust_session(self) -> None:
        self.trusted = True


def test_distance_m_is_reasonable_for_nearby_points() -> None:
    # Roughly 111 m per 0.001 degree latitude at the equator.
    assert 110 <= P.distance_m(0, 0, 0.001, 0) <= 112


def test_entity_from_device_normalizes_location_and_home_distance() -> None:
    device = type("Device", (), {})()
    device.data = {
        "id": "dev-1",
        "name": "Test Phone",
        "deviceDisplayName": "iPhone",
        "deviceClass": "iPhone",
        "batteryLevel": 0.57,
        "batteryStatus": "Charging",
        "location": {
            "latitude": 0.0,
            "longitude": 0.0,
            "horizontalAccuracy": 12.4,
            "timeStamp": 1_700_000_000_000,
        },
    }

    entity = P._entity_from_device(device, LocationConfig(lat=0.001, lon=0.0))

    assert entity.entity_id == "dev-1"
    assert entity.name == "Test Phone"
    assert entity.model == "iPhone"
    assert entity.device_class == "iPhone"
    assert entity.latitude == 0.0
    assert entity.longitude == 0.0
    assert entity.horizontal_accuracy_m == 12.4
    assert entity.battery_level_pct == 57
    assert entity.battery_status == "Charging"
    assert entity.last_seen is not None
    assert entity.last_seen.tzinfo == timezone.utc
    assert 100 <= entity.distance_from_home_m <= 120
    assert entity.at_home is True


def test_entity_from_device_marks_location_outside_home_radius_as_away() -> None:
    device = type("Device", (), {})()
    device.data = {
        "id": "dev-2",
        "name": "Away Phone",
        "location": {"latitude": 0.01, "longitude": 0.0},
    }

    entity = P._entity_from_device(
        device,
        LocationConfig(lat=0.0, lon=0.0),
        home_radius_m=200,
    )

    assert entity.distance_from_home_m is not None
    assert entity.distance_from_home_m > 1000
    assert entity.at_home is False


def test_entity_from_device_tolerates_missing_location() -> None:
    device = type("Device", (), {})()
    device.data = {"id": "tag-1", "name": "Keys", "batteryLevel": None}

    entity = P._entity_from_device(device)

    assert entity.name == "Keys"
    assert entity.has_location is False
    assert entity.latitude is None
    assert entity.battery_level_pct is None
    assert entity.at_home is None


def test_load_presence_config_reads_home_radius(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(P, "load_dotenv", lambda override=True: None)
    monkeypatch.setenv("ICLOUD_EMAIL", "fixture@example.com")
    monkeypatch.setenv("ICLOUD_PASSWORD", "secret")
    monkeypatch.setenv("PRESENCE_HOME_RADIUS_M", "150")
    monkeypatch.delenv("ICLOUD_EMAIL_2", raising=False)
    monkeypatch.delenv("ICLOUD_PASSWORD_2", raising=False)
    monkeypatch.delenv("ICLOUD_LABEL", raising=False)

    cfg = P.load_presence_config(session_dir=tmp_path)

    assert cfg.home_radius_m == 150
    assert cfg.session_dir == tmp_path
    assert cfg.label == "1"
    assert cfg.friendly_name == ""


def test_load_presence_configs_single_account_when_no_second(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(P, "load_dotenv", lambda override=True: None)
    monkeypatch.setenv("ICLOUD_EMAIL", "one@example.com")
    monkeypatch.setenv("ICLOUD_PASSWORD", "secret")
    monkeypatch.delenv("ICLOUD_EMAIL_2", raising=False)
    monkeypatch.delenv("ICLOUD_PASSWORD_2", raising=False)
    monkeypatch.delenv("ICLOUD_LABEL", raising=False)

    configs = P.load_presence_configs(primary_session_dir=tmp_path)

    assert len(configs) == 1
    assert configs[0].email == "one@example.com"
    assert configs[0].label == "1"
    assert configs[0].session_dir == tmp_path


def test_load_presence_configs_includes_second_account(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(P, "load_dotenv", lambda override=True: None)
    monkeypatch.setenv("ICLOUD_EMAIL", "one@example.com")
    monkeypatch.setenv("ICLOUD_PASSWORD", "secret1")
    monkeypatch.setenv("ICLOUD_EMAIL_2", "two@example.com")
    monkeypatch.setenv("ICLOUD_PASSWORD_2", "secret2")
    monkeypatch.setenv("ICLOUD_SESSION_DIR_2", "webapp/custom_session_2")
    monkeypatch.delenv("ICLOUD_LABEL", raising=False)
    monkeypatch.delenv("ICLOUD_LABEL_2", raising=False)

    configs = P.load_presence_configs()

    assert [c.email for c in configs] == ["one@example.com", "two@example.com"]
    assert [c.label for c in configs] == ["1", "2"]
    assert configs[1].session_dir == P.Path("webapp/custom_session_2")
    assert [c.friendly_name for c in configs] == ["", ""]


def test_load_presence_configs_reads_friendly_labels(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #655: an optional friendly per-account label for Telegram copy."""

    monkeypatch.setattr(P, "load_dotenv", lambda override=True: None)
    monkeypatch.setenv("ICLOUD_EMAIL", "one@example.com")
    monkeypatch.setenv("ICLOUD_PASSWORD", "secret1")
    monkeypatch.setenv("ICLOUD_LABEL", "Roberto")
    monkeypatch.setenv("ICLOUD_EMAIL_2", "two@example.com")
    monkeypatch.setenv("ICLOUD_PASSWORD_2", "secret2")
    monkeypatch.setenv("ICLOUD_LABEL_2", "Ana")

    configs = P.load_presence_configs()

    assert [c.friendly_name for c in configs] == ["Roberto", "Ana"]


def test_load_presence_configs_second_account_defaults_session_dir(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(P, "load_dotenv", lambda override=True: None)
    monkeypatch.setenv("ICLOUD_EMAIL", "one@example.com")
    monkeypatch.setenv("ICLOUD_PASSWORD", "secret1")
    monkeypatch.setenv("ICLOUD_EMAIL_2", "two@example.com")
    monkeypatch.setenv("ICLOUD_PASSWORD_2", "secret2")
    monkeypatch.delenv("ICLOUD_SESSION_DIR_2", raising=False)

    configs = P.load_presence_configs()

    assert configs[1].session_dir == P.DEFAULT_SESSION_DIR_2


def test_load_presence_configs_skips_partial_second_account(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(P, "load_dotenv", lambda override=True: None)
    monkeypatch.setenv("ICLOUD_EMAIL", "one@example.com")
    monkeypatch.setenv("ICLOUD_PASSWORD", "secret1")
    monkeypatch.setenv("ICLOUD_EMAIL_2", "two@example.com")
    monkeypatch.delenv("ICLOUD_PASSWORD_2", raising=False)  # password missing

    configs = P.load_presence_configs()

    assert len(configs) == 1
    assert configs[0].email == "one@example.com"


def test_load_presence_configs_requires_primary_account(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(P, "load_dotenv", lambda override=True: None)
    monkeypatch.delenv("ICLOUD_EMAIL", raising=False)
    monkeypatch.delenv("ICLOUD_PASSWORD", raising=False)

    with pytest.raises(P.PresenceConfigError, match="Missing iCloud credentials"):
        P.load_presence_configs()


def test_2fa_without_code_raises_actionable_error() -> None:
    api = _FakeApi(requires_2fa=True)

    with pytest.raises(P.PresenceAuthError, match="requires 2FA"):
        P._complete_2fa(api, verification_code=None, trust_session=True)


def test_2fa_with_code_validates_and_trusts_session() -> None:
    api = _FakeApi(requires_2fa=True)

    P._complete_2fa(api, verification_code="123456", trust_session=True)

    assert api.trusted is True
    assert api.requires_2fa is False


def test_iter_devices_refreshes_with_location() -> None:
    devices = _FakeDevices([object()])

    out = list(P._iter_devices(devices))

    assert len(out) == 1
    assert devices.refreshed is True


@pytest.fixture(autouse=True)
def _clear_service_cache() -> None:
    """Isolate each test's session cache — module-global by design (#651) —
    plus the untrusted-WARNING latch and the pending trust renewals (#659)."""

    P._SERVICE_CACHE.clear()
    P._UNTRUSTED_WARNED.clear()
    P._PENDING_TRUST.clear()
    yield
    P._SERVICE_CACHE.clear()
    P._UNTRUSTED_WARNED.clear()
    P._PENDING_TRUST.clear()


def test_connect_reuses_cached_session(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cfg = P.PresenceConfig(email="a@example.com", password="x", session_dir=tmp_path)
    build_calls = []

    def fake_build(config: P.PresenceConfig) -> object:
        build_calls.append(config)
        return _FakeApi()

    monkeypatch.setattr(P, "_build_service", fake_build)

    first = P._connect(cfg)
    second = P._connect(cfg)

    assert first is second
    assert len(build_calls) == 1


def test_connect_rebuilds_after_invalidation(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cfg = P.PresenceConfig(email="a@example.com", password="x", session_dir=tmp_path)
    build_calls = []

    def fake_build(config: P.PresenceConfig) -> object:
        build_calls.append(config)
        return _FakeApi()

    monkeypatch.setattr(P, "_build_service", fake_build)

    first = P._connect(cfg)
    P.invalidate_session(cfg)
    second = P._connect(cfg)

    assert first is not second
    assert len(build_calls) == 2


def test_fetch_presence_reuses_session_across_calls(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cfg = P.PresenceConfig(email="a@example.com", password="x", session_dir=tmp_path)
    api = _FakeApi()
    build_calls = []

    def fake_build(config: P.PresenceConfig) -> object:
        build_calls.append(config)
        return api

    monkeypatch.setattr(P, "_build_service", fake_build)
    monkeypatch.setattr(P, "load_location_config", lambda: None)

    P.fetch_presence(config=cfg)
    P.fetch_presence(config=cfg)

    assert len(build_calls) == 1


def test_fetch_presence_keeps_cached_session_on_generic_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A generic fetch failure must not evict the cached session (issue #656).

    Eviction is the expensive, user-visible operation (a full Apple sign-in
    handshake that can prompt a trusted-device approval) - unconditionally
    evicting on every failure here bypassed the presence refresher's
    backoff-gated self-heal entirely, forcing a fresh handshake on every poll
    while an account was flapping. Only an explicit ``invalidate_session()``
    call (made by that backoff-gated self-heal) may evict now.
    """

    cfg = P.PresenceConfig(email="a@example.com", password="x", session_dir=tmp_path)
    build_calls = []

    def fake_build(config: P.PresenceConfig) -> object:
        build_calls.append(config)
        return _FakeApi()

    monkeypatch.setattr(P, "_build_service", fake_build)
    monkeypatch.setattr(P, "load_location_config", lambda: None)

    def boom(devices: object) -> list[object]:
        raise RuntimeError("session expired")

    monkeypatch.setattr(P, "_iter_devices", boom)

    with pytest.raises(RuntimeError, match="session expired"):
        P.fetch_presence(config=cfg)

    assert str(cfg.session_dir) in P._SERVICE_CACHE

    monkeypatch.setattr(P, "_iter_devices", lambda devices: [])
    P.fetch_presence(config=cfg)

    assert len(build_calls) == 1


def test_fetch_presence_serves_cached_session_despite_poisoned_2fa_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """Issue #658 regression: pyicloud's in-memory ``requires_2fa`` is not a
    health signal. Its Find My sub-service flips it true on its own internal
    re-auth (expired browser trust) while still serving every device; gating
    on the flag reported working sessions as ``2fa_required`` on every poll
    after the first, which drove the 4-hourly forced re-auth + Telegram cycle.
    A fetch that succeeds is healthy - and the session stays cached."""

    cfg = P.PresenceConfig(email="a@example.com", password="x", session_dir=tmp_path)
    device = type("Device", (), {})()
    device.data = {"id": "dev-1", "name": "Phone"}
    api = _FakeApi(requires_2fa=True)
    api.devices = _FakeDevices([device])
    build_calls = []

    def fake_build(config: P.PresenceConfig) -> object:
        build_calls.append(config)
        return api

    monkeypatch.setattr(P, "_build_service", fake_build)
    monkeypatch.setattr(P, "load_location_config", lambda: None)
    P._UNTRUSTED_WARNED.discard(str(tmp_path))

    with caplog.at_level("WARNING", logger="presence"):
        first = P.fetch_presence(config=cfg)
        second = P.fetch_presence(config=cfg)

    assert [e.entity_id for e in first] == ["dev-1"]
    assert [e.entity_id for e in second] == ["dev-1"]
    assert api.devices.refreshed is True
    assert len(build_calls) == 1
    assert str(cfg.session_dir) in P._SERVICE_CACHE
    # Edge-triggered: one "untrusted session" warning per session build, not per poll.
    untrusted = [r for r in caplog.records if "untrusted session" in r.getMessage()]
    assert len(untrusted) == 1


def test_fetch_presence_maps_pyicloud_auth_failure_to_auth_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A fetch Apple actually refuses is what "really broken" means (#658):
    pyicloud's auth-flavoured exceptions become PresenceAuthError (so the
    refresher reports ``2fa_required`` and its backoff-gated self-heal
    applies) - and, per #656, the session is still not evicted here."""

    from pyicloud.exceptions import PyiCloudAuthRequiredException

    cfg = P.PresenceConfig(email="a@example.com", password="x", session_dir=tmp_path)
    build_calls = []

    def fake_build(config: P.PresenceConfig) -> object:
        build_calls.append(config)
        return _FakeApi()

    monkeypatch.setattr(P, "_build_service", fake_build)
    monkeypatch.setattr(P, "load_location_config", lambda: None)

    def refuse(devices: object) -> list[object]:
        raise PyiCloudAuthRequiredException("a@example.com", None)

    monkeypatch.setattr(P, "_iter_devices", refuse)

    with pytest.raises(P.PresenceAuthError, match="refused the session"):
        P.fetch_presence(config=cfg)

    assert str(cfg.session_dir) in P._SERVICE_CACHE
    assert len(build_calls) == 1


def test_is_auth_failure_only_matches_pyicloud_auth_exceptions() -> None:
    from pyicloud.exceptions import (
        PyiCloud2FARequiredException,
        PyiCloudFailedLoginException,
        PyiCloudNoDevicesException,
    )

    assert P._is_auth_failure(PyiCloud2FARequiredException("a@example.com", None))
    assert P._is_auth_failure(PyiCloudFailedLoginException("bad password"))
    assert not P._is_auth_failure(PyiCloudNoDevicesException())
    assert not P._is_auth_failure(RuntimeError("network down"))


def test_fetch_presence_applies_code_after_fetch_when_flag_flips_inside_devices(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The internal re-auth that needs the code happens *inside* the fetch
    (Find My 450 -> accountLogin -> SRP), so an explicit ``--2fa-code`` must be
    applied again afterwards or the browser trust is never renewed (#658)."""

    cfg = P.PresenceConfig(email="a@example.com", password="x", session_dir=tmp_path)

    class _FlippingApi(_FakeApi):
        @property
        def devices(self) -> _FakeDevices:
            self.requires_2fa = True  # pyicloud's sub-service poisons the flag mid-fetch
            return self._devices

        @devices.setter
        def devices(self, value: _FakeDevices) -> None:
            self._devices = value

    api = _FlippingApi(requires_2fa=False)
    monkeypatch.setattr(P, "_build_service", lambda config: api)
    monkeypatch.setattr(P, "load_location_config", lambda: None)

    P.fetch_presence(config=cfg, verification_code="123456")

    assert api.requires_2fa is False
    assert api.trusted is True


def test_service_class_quiet_variant_never_requests_2fa_push() -> None:
    """The unattended tray must not have pyicloud ask Apple to push a 2FA code
    (#658) - the attended CLI keeps pyicloud's real hook. The pinned
    ``pyicloud==2.6.5`` hook must still exist, or the override is dead code."""

    from pyicloud import PyiCloudService

    assert P._service_class(request_2fa_push=True) is PyiCloudService
    assert callable(getattr(PyiCloudService, "_request_2fa_code", None))

    quiet = P._service_class(request_2fa_push=False)
    assert issubclass(quiet, PyiCloudService)
    assert quiet._request_2fa_code is not PyiCloudService._request_2fa_code
    # The override touches nothing on the instance - a bare object stands in.
    quiet._request_2fa_code(object.__new__(quiet))


def test_assert_push_hook_present_fails_loud_when_pyicloud_renames_hook() -> None:
    class _NoHook:
        pass

    with pytest.raises(P.PresenceConfigError, match="_request_2fa_code"):
        P._assert_push_hook_present(_NoHook)


def test_build_service_uses_quiet_class_when_push_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    seen: list[bool] = []

    class _Recorder:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.kwargs = kwargs

    def fake_service_class(*, request_2fa_push: bool) -> type:
        seen.append(request_2fa_push)
        return _Recorder

    monkeypatch.setattr(P, "_service_class", fake_service_class)
    cfg = P.PresenceConfig(
        email="a@example.com", password="x", session_dir=tmp_path, request_2fa_push=False
    )

    api = P._build_service(cfg)

    assert seen == [False]
    assert api.kwargs["cookie_directory"] == str(tmp_path)


# ------------------------------------------------ browser-trust renewal (#659)
class _StopEvent:
    def __init__(self) -> None:
        self.stopped = False

    def set(self) -> None:
        self.stopped = True


class _FakeManager:
    """Stands in for pyicloud's lazily-built Find My manager (monitor thread)."""

    def __init__(self) -> None:
        self.stop_event = _StopEvent()

    def __len__(self) -> int:  # bool(manager) must never be consulted (network!)
        raise AssertionError("bool()/len() on the Find My manager triggers a refresh")


class _FakeTrustApi:
    """pyicloud through the renewal state machine: ``authenticate(force_refresh)``
    ends either challenged (2FA required, code pushed) or already trusted;
    ``validate_2fa_code`` accepts one code and - like the real 2.6.5, which
    calls ``trust_session()`` + a fresh ``accountLogin`` inside - refreshes
    the trust flags on success."""

    def __init__(self, *, challenge: bool = True, accept: str = "123456", trust_after: bool = True) -> None:
        self.challenge = challenge
        self.accept = accept
        self.trust_after = trust_after
        self.requires_2fa = False
        self.is_trusted_session = False
        self.two_factor_delivery_method = "trusted_device"
        self.calls: list[str] = []
        self._devices = None

    def authenticate(self, force_refresh: bool = False) -> None:
        self.calls.append("authenticate:%s" % force_refresh)
        self.requires_2fa = self.challenge
        self.is_trusted_session = not self.challenge

    def validate_2fa_code(self, code: str) -> bool:
        self.calls.append("validate")
        if code != self.accept:
            return False
        if self.trust_after:
            self.requires_2fa = False
            self.is_trusted_session = True
            return True
        return False


def _trust_cfg(tmp_path, **overrides) -> P.PresenceConfig:
    return P.PresenceConfig(
        email="a@example.com", password="x", session_dir=tmp_path, label="1", **overrides
    )


def test_begin_trust_renewal_parks_challenged_service_and_reports_code_sent(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    api = _FakeTrustApi()
    built: list[P.PresenceConfig] = []

    def fake_build(config: P.PresenceConfig) -> object:
        built.append(config)
        return api

    monkeypatch.setattr(P, "_build_service", fake_build)
    # Even a config the unattended refresher opted out of pushes for: this is
    # the attended flow, the push is wanted.
    cfg = _trust_cfg(tmp_path, request_2fa_push=False)

    state = P.begin_trust_renewal(cfg)

    assert state.status == "code_sent"
    assert state.trusted is False
    assert "trusted devices" in state.detail
    assert built[0].request_2fa_push is True
    assert api.calls == ["authenticate:True"]
    assert P._PENDING_TRUST[str(tmp_path)].api is api
    # Only a *completed* renewal touches the tray's cache.
    assert P._SERVICE_CACHE == {}


def test_begin_trust_renewal_does_not_force_a_second_sign_in_on_a_fresh_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A brand-new session dir has no token: the build itself already ran SRP
    and pushed the code, so forcing another sign-in would push twice."""

    class _ChallengedAtBuild(_FakeTrustApi):
        def __init__(self) -> None:
            super().__init__()
            self.requires_2fa = True  # pyicloud's constructor already hit 2FA

    api = _ChallengedAtBuild()
    monkeypatch.setattr(P, "_build_service", lambda config: api)

    state = P.begin_trust_renewal(_trust_cfg(tmp_path))

    assert state.status == "code_sent"
    assert api.calls == []  # no authenticate(force_refresh=True) on top
    assert P._PENDING_TRUST[str(tmp_path)].api is api


def test_begin_trust_renewal_sms_delivery_is_named(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    api = _FakeTrustApi()
    api.two_factor_delivery_method = "sms"
    monkeypatch.setattr(P, "_build_service", lambda config: api)

    state = P.begin_trust_renewal(_trust_cfg(tmp_path))

    assert state.status == "code_sent"
    assert "SMS" in state.detail


def test_complete_trust_renewal_replaces_cache_and_clears_untrusted_latch(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The happy path: code accepted -> trusted service becomes the account's
    cached session (the tray's next poll reuses it), the retired service's
    Find My monitor thread is stopped so it cannot re-save the old, untrusted
    session data over the new trust token, the WARNING latch is cleared, and
    the pending challenge is consumed."""

    cfg = _trust_cfg(tmp_path)
    key = str(tmp_path)
    old = _FakeTrustApi()
    old._devices = _FakeManager()
    P._SERVICE_CACHE[key] = old
    P._UNTRUSTED_WARNED.add(key)
    fresh = _FakeTrustApi()
    monkeypatch.setattr(P, "_build_service", lambda config: fresh)

    assert P.begin_trust_renewal(cfg).status == "code_sent"
    state = P.complete_trust_renewal(cfg, "123456")

    assert state.status == "trusted"
    assert state.trusted is True
    assert P._SERVICE_CACHE[key] is fresh
    assert old._devices.stop_event.stopped is True
    assert key not in P._UNTRUSTED_WARNED
    assert key not in P._PENDING_TRUST
    assert P.session_trust_state(cfg) is True


def test_complete_trust_renewal_invalid_code_allows_one_retry_then_needs_new_begin(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    cfg = _trust_cfg(tmp_path)
    fresh = _FakeTrustApi()
    monkeypatch.setattr(P, "_build_service", lambda config: fresh)
    P.begin_trust_renewal(cfg)

    first = P.complete_trust_renewal(cfg, "000000")
    assert first.status == "invalid_code"
    assert "try once more" in first.detail
    assert str(tmp_path) in P._PENDING_TRUST  # same challenge, retry allowed

    second = P.complete_trust_renewal(cfg, "000000")
    assert second.status == "invalid_code"
    assert "start again" in second.detail
    assert str(tmp_path) not in P._PENDING_TRUST  # challenge discarded

    third = P.complete_trust_renewal(cfg, "123456")
    assert third.status == "expired"
    assert P._SERVICE_CACHE == {}


def test_complete_trust_renewal_without_begin_or_after_ttl_is_expired(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    cfg = _trust_cfg(tmp_path)
    assert P.complete_trust_renewal(cfg, "123456").status == "expired"

    fresh = _FakeTrustApi()
    monkeypatch.setattr(P, "_build_service", lambda config: fresh)
    P.begin_trust_renewal(cfg)
    P._PENDING_TRUST[str(tmp_path)].started_at -= P.PENDING_TRUST_TTL_S + 1

    state = P.complete_trust_renewal(cfg, "123456")

    assert state.status == "expired"
    assert "expired" in state.detail
    assert str(tmp_path) not in P._PENDING_TRUST
    assert fresh.calls == ["authenticate:True"]  # the code was never sent to Apple


def test_complete_trust_renewal_reports_failed_when_trust_does_not_stick(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A code Apple accepts but a trust it does not grant is neither a wrong
    code nor success - distinct state, cache untouched, challenge consumed."""

    cfg = _trust_cfg(tmp_path)

    class _AcceptsButUntrusted(_FakeTrustApi):
        def validate_2fa_code(self, code: str) -> bool:
            self.calls.append("validate")
            self.requires_2fa = True  # accountLogin inside trust_session said: still untrusted
            return True

    fresh = _AcceptsButUntrusted()
    monkeypatch.setattr(P, "_build_service", lambda config: fresh)
    P.begin_trust_renewal(cfg)

    state = P.complete_trust_renewal(cfg, "123456")

    assert state.status == "failed"
    assert state.trusted is False
    assert P._SERVICE_CACHE == {}
    assert str(tmp_path) not in P._PENDING_TRUST


def test_begin_trust_renewal_already_trusted_adopts_fresh_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    cfg = _trust_cfg(tmp_path)
    key = str(tmp_path)
    P._UNTRUSTED_WARNED.add(key)
    fresh = _FakeTrustApi(challenge=False)
    monkeypatch.setattr(P, "_build_service", lambda config: fresh)

    state = P.begin_trust_renewal(cfg)

    assert state.status == "already_trusted"
    assert state.trusted is True
    assert P._SERVICE_CACHE[key] is fresh
    assert key not in P._UNTRUSTED_WARNED
    assert key not in P._PENDING_TRUST


def test_begin_trust_renewal_maps_apple_refusal_to_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from pyicloud.exceptions import PyiCloudFailedLoginException

    def refuse(config: P.PresenceConfig) -> object:
        raise PyiCloudFailedLoginException("Invalid email/password combination.")

    monkeypatch.setattr(P, "_build_service", refuse)

    state = P.begin_trust_renewal(_trust_cfg(tmp_path))

    assert state.status == "failed"
    assert "Invalid email/password" in state.detail
    assert P._PENDING_TRUST == {}


def test_trust_renewal_never_logs_the_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = _trust_cfg(tmp_path)
    monkeypatch.setattr(P, "_build_service", lambda config: _FakeTrustApi(accept="654321"))
    with caplog.at_level("DEBUG", logger=P.logger.name):
        P.begin_trust_renewal(cfg)
        P.complete_trust_renewal(cfg, "111111")  # rejected
        P.complete_trust_renewal(cfg, "654321")  # accepted

    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "111111" not in joined and "654321" not in joined
    assert "browser trust renewed" in joined


def test_session_trust_state_is_none_without_a_cached_session(tmp_path) -> None:
    cfg = _trust_cfg(tmp_path)
    assert P.session_trust_state(cfg) is None
    api = _FakeApi(requires_2fa=True)
    P._SERVICE_CACHE[str(tmp_path)] = api
    assert P.session_trust_state(cfg) is False
    api.requires_2fa = False
    assert P.session_trust_state(cfg) is True
