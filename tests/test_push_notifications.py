from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

import src.push_notifications as push


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _gen_raw_private_key() -> str:
    """A fresh VAPID private key in the format the fixed key-gen script writes."""
    private = ec.generate_private_key(ec.SECP256R1())
    raw = private.private_numbers().private_value.to_bytes(32, "big")
    return _b64url(raw)


def _gen_pem_private_key() -> str:
    """A fresh VAPID private key in the old (broken) PEM-string format (#284)."""
    private = ec.generate_private_key(ec.SECP256R1())
    return private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


@pytest.fixture(autouse=True)
def _reset_validation_cache():
    """Each test gets its own cache state — validate_push_config() caches by key value."""
    push._validated_private_key = None
    push._validated_private_key_ok = False
    yield
    push._validated_private_key = None
    push._validated_private_key_ok = False


def test_validate_push_config_not_configured_is_not_an_error() -> None:
    assert push.validate_push_config({"public_key": "", "private_key": "", "subject": "mailto:x@example.com"}) is True


def test_validate_push_config_accepts_raw_b64url_scalar() -> None:
    cfg = {"public_key": "pub", "private_key": _gen_raw_private_key(), "subject": "mailto:x@example.com"}
    assert push.validate_push_config(cfg) is True


def test_validate_push_config_rejects_pem_string(caplog: pytest.LogCaptureFixture) -> None:
    """Reproduces #284: a full PEM string fails ASN.1 parsing when passed as a plain string."""
    cfg = {"public_key": "pub", "private_key": _gen_pem_private_key(), "subject": "mailto:x@example.com"}
    with caplog.at_level("WARNING", logger="src.push_notifications"):
        assert push.validate_push_config(cfg) is False
    assert sum("pushes disabled" in r.message for r in caplog.records) == 1


def test_validate_push_config_caches_and_logs_only_once(caplog: pytest.LogCaptureFixture) -> None:
    cfg = {"public_key": "pub", "private_key": _gen_pem_private_key(), "subject": "mailto:x@example.com"}
    with caplog.at_level("WARNING", logger="src.push_notifications"):
        assert push.validate_push_config(cfg) is False
        assert push.validate_push_config(cfg) is False
        assert push.validate_push_config(cfg) is False
    assert sum("pushes disabled" in r.message for r in caplog.records) == 1


def test_send_push_short_circuits_on_unreadable_key(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """An unreadable key must disable sending entirely, not fail per-subscription."""
    bad_cfg = {"public_key": "pub", "private_key": _gen_pem_private_key(), "subject": "mailto:x@example.com"}
    monkeypatch.setattr(push, "load_push_config", lambda *a, **k: bad_cfg)

    subs_path = tmp_path / "push_subscriptions.json"
    monkeypatch.setattr(push, "SUBSCRIPTIONS_PATH", subs_path)
    push.save_subscription(
        {"endpoint": "https://push.example/sub", "keys": {"p256dh": "x", "auth": "y"}},
        path=subs_path,
    )

    called = False

    def _fail_if_called(*_a, **_k):
        nonlocal called
        called = True
        raise AssertionError("webpush() must not be called when the VAPID key is unreadable")

    monkeypatch.setattr("pywebpush.webpush", _fail_if_called)

    assert push.send_push("title", "body") == 0
    assert called is False


def test_send_push_works_with_valid_raw_key_config_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity: a correctly-formatted key config passes validation and reaches the send loop."""
    good_cfg = {"public_key": "pub", "private_key": _gen_raw_private_key(), "subject": "mailto:x@example.com"}
    monkeypatch.setattr(push, "load_push_config", lambda *a, **k: good_cfg)
    monkeypatch.setattr(push, "load_subscriptions", lambda *a, **k: [])

    # No subscriptions -> short-circuits after validation succeeds, still 0 sent.
    assert push.send_push("title", "body") == 0
