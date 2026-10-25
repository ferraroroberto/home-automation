"""Unit tests for the short-lived HMAC-signed camera stream token."""

from __future__ import annotations

from src.camera_token import issue, verify


def test_issue_returns_expected_shape() -> None:
    result = issue("my-bearer")
    assert "token" in result and "expires_in" in result
    assert result["expires_in"] == 60
    assert "." in result["token"]


def test_verify_accepts_fresh_token() -> None:
    result = issue("my-bearer")
    assert verify(result["token"], "my-bearer") is True


def test_verify_rejects_wrong_bearer() -> None:
    result = issue("my-bearer")
    assert verify(result["token"], "wrong-bearer") is False


def test_verify_rejects_expired_token() -> None:
    result = issue("my-bearer")
    ts_str, sig = result["token"].split(".", 1)
    # Rewind the expiry by TTL+1 seconds so the token is past its window.
    expired = str(int(ts_str) - 61) + "." + sig
    assert verify(expired, "my-bearer") is False


def test_verify_rejects_garbage() -> None:
    assert verify("notavalidtoken", "my-bearer") is False
    assert verify("", "my-bearer") is False
    assert verify("123.wronghmac", "my-bearer") is False


def test_verify_rejects_tampered_expiry() -> None:
    """Extending the expiry timestamp invalidates the HMAC."""
    result = issue("my-bearer")
    ts_str, sig = result["token"].split(".", 1)
    tampered = str(int(ts_str) + 9999) + "." + sig
    assert verify(tampered, "my-bearer") is False
