"""API coverage for the dated export-compensation editor (issue #718)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _isolate_tariff(monkeypatch, tmp_path):
    import src.tariff as tariff

    path = tmp_path / "tariff.json"
    path.write_text(json.dumps({
        "currency": "EUR",
        "tariff_name": "Test",
        "calendar": "flat",
        "periods": {"FLAT": {"label": "Flat", "price_eur_kwh": 0.1}},
        "export_eur_kwh": 0.04,
    }), encoding="utf-8")
    monkeypatch.setattr(tariff, "DEFAULT_PATH", path)
    return path


def test_get_exposes_legacy_rate(client: TestClient) -> None:
    response = client.get("/api/energy/export-rates")

    assert response.status_code == 200
    assert response.json()["rates"] == [
        {"effective_from": "0001-01-01", "export_eur_kwh": 0.04},
    ]


def test_put_adds_rate_and_migrates_legacy_shape(
    client: TestClient, _isolate_tariff
) -> None:
    response = client.put("/api/energy/export-rates", json={
        "effective_from": "2026-09-05", "export_eur_kwh": 0.16774,
    })

    assert response.status_code == 200
    assert response.json()["current_export_eur_kwh"] == 0.16774
    stored = json.loads(_isolate_tariff.read_text(encoding="utf-8"))
    assert "export_eur_kwh" not in stored
    assert stored["export_rates"][-1] == {
        "effective_from": "2026-09-05", "export_eur_kwh": 0.16774,
    }


def test_put_edits_date_and_delete_removes_entry(client: TestClient) -> None:
    client.put("/api/energy/export-rates", json={
        "effective_from": "2026-09-05", "export_eur_kwh": 0.10,
    })
    edited = client.put("/api/energy/export-rates", json={
        "effective_from": "2026-09-06", "replace_effective_from": "2026-09-05",
        "export_eur_kwh": 0.20, "hourly_eur_kwh": [0.21] + [None] * 23,
    })

    assert edited.status_code == 200
    assert not any(rate["effective_from"] == "2026-09-05" for rate in edited.json()["rates"])
    assert edited.json()["rates"][-1]["hourly_eur_kwh"][0] == 0.21

    deleted = client.delete("/api/energy/export-rates?effective_from=2026-09-06")
    assert deleted.status_code == 200
    assert not any(rate["effective_from"] == "2026-09-06" for rate in deleted.json()["rates"])


@pytest.mark.parametrize("payload, field", [
    ({"effective_from": "not-a-date", "export_eur_kwh": 0.1}, "effective_from"),
    ({"effective_from": "2026-09-05", "export_eur_kwh": -1}, "export_eur_kwh"),
])
def test_put_rejects_invalid_fields(client: TestClient, payload, field: str) -> None:
    response = client.put("/api/energy/export-rates", json=payload)

    assert response.status_code == 400
    assert field in response.json()["detail"]
