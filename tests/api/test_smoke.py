"""Python-level boot/health smoke against the real FastAPI app.

Asserts the app imports + wires up and that the credential-free / static
endpoints answer. Every domain-specific route test has moved to its own
``tests/api/test_<domain>_api.py`` (issue #723) — this file now holds only
the genuine boot smoke.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_healthz(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_version_shape(client: TestClient) -> None:
    resp = client.get("/api/version")
    assert resp.status_code == 200
    body = resp.json()
    # Build identity the PWA footer + restart-recipe check rely on.
    assert "git_sha" in body and "built_at" in body
    assert isinstance(body["git_sha"], str) and body["git_sha"]


def test_index_serves_html(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
