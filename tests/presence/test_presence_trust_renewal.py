"""Unit tests for :mod:`src.presence_client`'s browser-trust renewal and its
Apple-terms and failure-detail paths.

Every Apple exchange is faked; no real credentials or network calls.
"""

from __future__ import annotations


import pytest

from src import presence_client as P
from tests.presence.test_presence_client import _FakeApi


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


def test_adopted_service_is_demoted_to_no_push_class(tmp_path) -> None:
    """A renewed session lives in the unattended tray afterwards, so the
    attended (pushing) service must not stay pushing once adopted (#659)."""

    from pyicloud import PyiCloudService

    cfg = P.PresenceConfig(email="a@example.com", password="x", session_dir=tmp_path)
    api = object.__new__(PyiCloudService)  # real class, no sign-in

    P._adopt_service(cfg, api)

    assert P._SERVICE_CACHE[str(tmp_path)] is api
    assert isinstance(api, PyiCloudService)
    assert type(api) is not PyiCloudService
    assert type(api)._request_2fa_code is not PyiCloudService._request_2fa_code
    P._SERVICE_CACHE.pop(str(tmp_path), None)


class _FakeBridgeTrustApi(_FakeTrustApi):
    """Issue #662: pyicloud's trusted-device bridge can return False from
    validate_2fa_code while Apple has already accepted the approval; a
    follow-up trust_session() (token re-login, no push) then proves trust."""

    def __init__(self, *, apple_granted: bool, trust_raises: bool = False) -> None:
        super().__init__(accept="never-matches")
        self.apple_granted = apple_granted
        self.trust_raises = trust_raises

    def trust_session(self) -> bool:
        self.calls.append("trust_session")
        if self.trust_raises:
            raise RuntimeError("2sv/trust 4xx")
        if self.apple_granted:
            self.requires_2fa = False
            self.is_trusted_session = True
            return True
        return False


def test_complete_trust_renewal_rejected_client_side_but_apple_granted_trust(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = _trust_cfg(tmp_path)
    api = _FakeBridgeTrustApi(apple_granted=True)
    monkeypatch.setattr(P, "_build_service", lambda config: api)
    P.begin_trust_renewal(cfg)

    with caplog.at_level("INFO", logger="presence"):
        state = P.complete_trust_renewal(cfg, "111111")

    assert state.status == "trusted"
    assert state.trusted is True
    assert P._SERVICE_CACHE[str(tmp_path)] is api
    assert str(tmp_path) not in P._PENDING_TRUST
    assert api.calls[-2:] == ["validate", "trust_session"]
    msgs = [r.getMessage() for r in caplog.records]
    assert any("granted browser trust" in m for m in msgs)
    assert any("verifying 2FA code via legacy endpoint" in m for m in msgs)
    assert not any("111111" in m for m in msgs)
    P._SERVICE_CACHE.pop(str(tmp_path), None)


@pytest.mark.parametrize("trust_raises", [False, True])
def test_complete_trust_renewal_rejected_and_apple_confirms_no_trust_stays_invalid(
    monkeypatch: pytest.MonkeyPatch, tmp_path, trust_raises: bool
) -> None:
    cfg = _trust_cfg(tmp_path)
    api = _FakeBridgeTrustApi(apple_granted=False, trust_raises=trust_raises)
    monkeypatch.setattr(P, "_build_service", lambda config: api)
    P.begin_trust_renewal(cfg)

    state = P.complete_trust_renewal(cfg, "111111")

    assert state.status == "invalid_code"
    assert "try once more" in state.detail
    assert str(tmp_path) in P._PENDING_TRUST  # still retryable
    assert P._PENDING_TRUST[str(tmp_path)].attempts == 1
    assert P._SERVICE_CACHE == {}
    P._discard_pending(str(tmp_path))


# ---------------------------------------------- Apple "accept updated terms" (#736)
def _terms_exc() -> Exception:
    from pyicloud.exceptions import PyiCloudAcceptTermsException

    # pyicloud's own wording, which prescribes the CLI flag this app must never use.
    return PyiCloudAcceptTermsException(
        "You must accept the updated terms of service to continue. "
        "Set --accept-terms to accept them."
    )


def test_fetch_presence_maps_terms_refusal_at_sign_in_to_terms_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Apple flags ``termsUpdateNeeded`` during the sign-in handshake: that is
    its own condition, not an auth failure or a generic error, and the message
    names the account holder's remedy instead of pyicloud's ``--accept-terms``."""

    cfg = P.PresenceConfig(email="a@example.com", password="x", session_dir=tmp_path)

    def refuse_build(config: P.PresenceConfig) -> object:
        raise _terms_exc()

    monkeypatch.setattr(P, "_build_service", refuse_build)
    monkeypatch.setattr(P, "load_location_config", lambda: None)

    with pytest.raises(P.PresenceTermsError) as info:
        P.fetch_presence(config=cfg)

    assert not isinstance(info.value, P.PresenceAuthError)
    assert "icloud.com" in str(info.value)
    assert "--accept-terms" not in str(info.value)
    assert str(cfg.session_dir) not in P._SERVICE_CACHE


def test_fetch_presence_maps_terms_refusal_inside_find_my_reauth(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """On an already-cached session the flag surfaces from Find My's internal
    re-auth, i.e. from the fetch itself - same distinct error there."""

    cfg = P.PresenceConfig(email="a@example.com", password="x", session_dir=tmp_path)
    monkeypatch.setattr(P, "_build_service", lambda config: _FakeApi())
    monkeypatch.setattr(P, "load_location_config", lambda: None)

    def refuse(devices: object) -> list[object]:
        raise _terms_exc()

    monkeypatch.setattr(P, "_iter_devices", refuse)

    with pytest.raises(P.PresenceTermsError):
        P.fetch_presence(config=cfg)


def test_terms_refusal_is_not_an_auth_failure() -> None:
    assert P._is_terms_failure(_terms_exc())
    assert not P._is_auth_failure(_terms_exc())
    assert not P._is_terms_failure(RuntimeError("network down"))


def test_begin_trust_renewal_reports_terms_required_not_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A trust renewal re-signs in and hits the same refusal: say so plainly
    rather than a generic failure that implies retrying might help."""

    def refuse_build(config: P.PresenceConfig) -> object:
        raise _terms_exc()

    monkeypatch.setattr(P, "_build_service", refuse_build)

    state = P.begin_trust_renewal(_trust_cfg(tmp_path))

    assert state.status == "terms_required"
    assert state.detail == P.TERMS_REQUIRED_DETAIL
    assert str(tmp_path) not in P._PENDING_TRUST


def test_complete_trust_renewal_reports_terms_required(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    api = _FakeTrustApi()

    def refuse_code(code: str) -> bool:
        raise _terms_exc()

    api.validate_2fa_code = refuse_code  # type: ignore[method-assign]
    monkeypatch.setattr(P, "_build_service", lambda config: api)
    cfg = _trust_cfg(tmp_path)
    assert P.begin_trust_renewal(cfg).status == "code_sent"

    state = P.complete_trust_renewal(cfg, "123456")

    assert state.status == "terms_required"
    assert "--accept-terms" not in state.detail


def test_no_code_path_accepts_apple_terms_on_the_account_holders_behalf() -> None:
    """Consent line (#736): accepting Apple's terms is the account holder's call.
    Nothing in the app may build pyicloud with ``accept_terms=True`` or shell out
    to its ``--accept-terms`` CLI flag."""

    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    offenders = []
    for folder in ("src", "app", "scripts", "custom_components"):
        for path in (root / folder).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if re.search(r"accept_terms\s*=\s*True|--accept-terms", text):
                offenders.append(str(path.relative_to(root)))
    assert offenders == []


_HTML_409_BODY = (
    '<script type="text/stache" id="jstache_fixture"> <div class="graphite-icon '
    '{{symbolName}} {{symbolType}}{{#if wrapperClasses}} {{wrapperClasses}}{{/if}}">'
    "</div></script>"
)


class _FakeHttpResponse:
    """The one attribute pyicloud's exception reads off a response."""

    def __init__(self, text: str) -> None:
        self.text = text


def _apple_409() -> Exception:
    """The real exception shape behind #966: message, code, then Apple's raw body."""

    from pyicloud.exceptions import PyiCloudAPIResponseException

    return PyiCloudAPIResponseException(
        "Authentication required for Account.", 409, _FakeHttpResponse(_HTML_409_BODY)
    )


def _assert_sheet_text_is_markup_free(detail: str) -> None:
    for leaked in ("<", "{{", "stache", "graphite"):
        assert leaked not in detail
    assert detail == "Authentication required for Account. (409)"


def test_begin_trust_renewal_failure_detail_carries_no_markup(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """#966: pyicloud appends Apple's HTML response body to its exception text;
    the Renew-trust sheet shows ``detail`` verbatim, so it must be stripped."""

    def refuse_build(config: P.PresenceConfig) -> object:
        raise _apple_409()

    monkeypatch.setattr(P, "_build_service", refuse_build)

    state = P.begin_trust_renewal(_trust_cfg(tmp_path))

    assert state.status == "failed"
    _assert_sheet_text_is_markup_free(state.detail)


def test_complete_trust_renewal_failure_detail_carries_no_markup(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    api = _FakeTrustApi()

    def refuse_code(code: str) -> bool:
        raise _apple_409()

    api.validate_2fa_code = refuse_code  # type: ignore[method-assign]
    monkeypatch.setattr(P, "_build_service", lambda config: api)
    cfg = _trust_cfg(tmp_path)
    assert P.begin_trust_renewal(cfg).status == "code_sent"

    state = P.complete_trust_renewal(cfg, "123456")

    assert state.status == "failed"
    _assert_sheet_text_is_markup_free(state.detail)


def test_apple_detail_with_nothing_readable_falls_back_to_the_neutral_message() -> None:
    assert P._apple_detail(RuntimeError(_HTML_409_BODY)) == (
        "Apple did not accept the sign-in; try again in a few minutes."
    )
