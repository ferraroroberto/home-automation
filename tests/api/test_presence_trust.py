"""API smoke for the iCloud browser-trust renewal endpoints (issue #659).

``POST /api/presence/icloud/{account}/trust/begin`` and ``…/complete`` drive
:mod:`src.presence_client`'s two-step renewal on the tray's live pyicloud
session. The client functions are monkeypatched here — nothing touches Apple —
so these prove the routing, validation, and payload shape only.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from app.webapp.presence_refresher import PresenceDiagnosticsCache
from src.presence_client import PresenceConfig, PresenceConfigError, TrustRenewalState

ROUTER = "app.webapp.routers.presence_trust"


def _configs() -> List[PresenceConfig]:
    return [
        PresenceConfig(email="one@example.com", password="x", label="1", friendly_name="Fixture One"),
        PresenceConfig(email="two@example.com", password="x", label="2"),
    ]


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """Two fake accounts, recording client calls; refresh_once is a no-op."""

    calls: Dict[str, Any] = {"begin": [], "complete": [], "refresh": 0}
    monkeypatch.setattr(f"{ROUTER}.load_presence_configs", _configs)

    async def fake_refresh() -> PresenceDiagnosticsCache:
        calls["refresh"] += 1
        return PresenceDiagnosticsCache(entities=[])

    monkeypatch.setattr(f"{ROUTER}.refresh_once", fake_refresh)
    return calls


def test_begin_returns_code_sent_payload_for_known_account(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, wired: Dict[str, Any]
) -> None:
    def fake_begin(config: PresenceConfig) -> TrustRenewalState:
        wired["begin"].append(config.label)
        return TrustRenewalState("code_sent", detail="Apple pushed a 6-digit code.", trusted=False)

    monkeypatch.setattr(f"{ROUTER}.begin_trust_renewal", fake_begin)

    res = client.post("/api/presence/icloud/1/trust/begin")

    assert res.status_code == 200
    assert res.json() == {
        "account": "1",
        "display_name": "Fixture One",
        "status": "code_sent",
        "detail": "Apple pushed a 6-digit code.",
        "trusted": False,
    }
    assert wired["begin"] == ["1"]
    assert wired["refresh"] == 0  # nothing to re-poll until the code is verified


def test_begin_already_trusted_repolls_diagnostics(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, wired: Dict[str, Any]
) -> None:
    monkeypatch.setattr(
        f"{ROUTER}.begin_trust_renewal",
        lambda config: TrustRenewalState("already_trusted", detail="still trusted", trusted=True),
    )

    body = client.post("/api/presence/icloud/2/trust/begin").json()

    assert body["status"] == "already_trusted"
    assert body["display_name"] == "two@example.com"  # no friendly name -> Apple ID
    assert wired["refresh"] == 1


def test_begin_unknown_account_is_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, wired: Dict[str, Any]
) -> None:
    monkeypatch.setattr(
        f"{ROUTER}.begin_trust_renewal",
        lambda config: pytest.fail("client must not be called for an unknown account"),
    )

    res = client.post("/api/presence/icloud/9/trust/begin")

    assert res.status_code == 404
    assert "Unknown iCloud account" in res.json()["detail"]


def test_begin_not_configured_is_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_unconfigured() -> List[PresenceConfig]:
        raise PresenceConfigError("Missing iCloud credentials.")

    monkeypatch.setattr(f"{ROUTER}.load_presence_configs", raise_unconfigured)

    res = client.post("/api/presence/icloud/1/trust/begin")

    assert res.status_code == 404
    assert "No iCloud account configured" in res.json()["detail"]


def test_complete_trusted_repolls_and_returns_state(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, wired: Dict[str, Any]
) -> None:
    def fake_complete(config: PresenceConfig, code: str) -> TrustRenewalState:
        wired["complete"].append((config.label, code))
        return TrustRenewalState("trusted", detail="Browser trust renewed.", trusted=True)

    monkeypatch.setattr(f"{ROUTER}.complete_trust_renewal", fake_complete)

    res = client.post("/api/presence/icloud/1/trust/complete", json={"code": "123 456"})

    assert res.status_code == 200
    assert res.json() == {
        "account": "1",
        "display_name": "Fixture One",
        "status": "trusted",
        "detail": "Browser trust renewed.",
        "trusted": True,
    }
    assert wired["complete"] == [("1", "123456")]  # spaces stripped before the client sees it
    assert wired["refresh"] == 1


@pytest.mark.parametrize("status", ["invalid_code", "expired", "failed"])
def test_complete_non_success_states_pass_through_without_repoll(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, wired: Dict[str, Any], status: str
) -> None:
    monkeypatch.setattr(
        f"{ROUTER}.complete_trust_renewal",
        lambda config, code: TrustRenewalState(status, detail=f"detail for {status}", trusted=False),
    )

    body = client.post("/api/presence/icloud/1/trust/complete", json={"code": "000000"}).json()

    assert body["status"] == status
    assert body["detail"] == f"detail for {status}"
    assert wired["refresh"] == 0


@pytest.mark.parametrize("payload", [{"code": "12345"}, {"code": "abcdef"}, {"code": ""}, {}])
def test_complete_rejects_malformed_code_before_touching_the_client(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, wired: Dict[str, Any], payload: Dict[str, str]
) -> None:
    monkeypatch.setattr(
        f"{ROUTER}.complete_trust_renewal",
        lambda config, code: pytest.fail("client must not be called for a malformed code"),
    )

    res = client.post("/api/presence/icloud/1/trust/complete", json=payload)

    assert res.status_code in (400, 422)
    assert wired["refresh"] == 0


def test_complete_unknown_account_is_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, wired: Dict[str, Any]
) -> None:
    res = client.post("/api/presence/icloud/3/trust/complete", json={"code": "123456"})

    assert res.status_code == 404
