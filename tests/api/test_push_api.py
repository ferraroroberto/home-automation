"""API smoke for the web-push subscription endpoint.

``POST /api/push/subscriptions`` persists a browser push subscription.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def test_push_subscription_endpoint_accepts_first_subscription(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import src.push_notifications as push

    store = tmp_path / "push_subscriptions.json"
    monkeypatch.setattr(push, "SUBSCRIPTIONS_PATH", store)

    resp = client.post(
        "/api/push/subscriptions",
        json={
            "endpoint": "https://push.example/sub",
            "keys": {"p256dh": "fixture", "auth": "secret"},
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "count": 1}
    assert push.load_subscriptions() == [
        {
            "endpoint": "https://push.example/sub",
            "keys": {"p256dh": "fixture", "auth": "secret"},
        }
    ]
