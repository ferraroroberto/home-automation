"""Non-ASCII secrets must compare cleanly (#833).

``hmac.compare_digest(str, str)`` raises ``TypeError`` on a non-ASCII ``str``,
so a password like ``contraseña`` could never log in (500) and a non-ASCII
token or webhook secret 500'd instead of 401'ing.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from src.secret_compare import secrets_match

_PASSWORD = "contraseña-é"


def test_secrets_match_handles_non_ascii() -> None:
    assert secrets_match(_PASSWORD, _PASSWORD) is True
    assert secrets_match(_PASSWORD, "contrasena-e") is False
    assert secrets_match("ñ", "n") is False
    assert secrets_match("abc", "abc") is True
    assert secrets_match("", "") is True


@pytest.fixture()
def remote(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from app.webapp.server import app as _app

    cfg = _app.state.webapp_config
    monkeypatch.setattr(cfg, "auth_token", "test-bearer-xyz")
    monkeypatch.setattr(cfg, "auth_password", _PASSWORD)
    return TestClient(_app, client=("192.0.2.1", 12345))


def test_login_with_non_ascii_password(remote: TestClient) -> None:
    ok = remote.post("/api/login", json={"password": _PASSWORD})
    assert ok.status_code == 200
    assert ok.json() == {"token": "test-bearer-xyz"}

    bad = remote.post("/api/login", json={"password": "contraseña-x"})
    assert bad.status_code == 401


def test_non_ascii_bearer_query_token_is_401(remote: TestClient) -> None:
    resp = remote.get("/api/units", params={"token": "tokeñ"})
    assert resp.status_code == 401


def test_non_ascii_webhook_secret_is_401(
    remote: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.webapp.routers.presence._webhook_secret", lambda: "s3cret")
    resp = remote.post(
        "/api/presence/webhook",
        params={"secret": "sécret"},
        json={"person_id": "p", "state": "home"},
    )
    assert resp.status_code == 401


def test_non_ascii_camera_token_is_rejected() -> None:
    from src.camera_token import verify

    assert verify(f"{int(time.time()) + 60}.sigñ", "test-bearer-xyz") is False
