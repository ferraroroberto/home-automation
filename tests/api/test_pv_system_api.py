"""API smoke for the Energy-tab PV-system editor (issue #561).

``GET/PUT /api/energy/pv-system`` round-trips the panel rows + shared derate
that the solar forecast is computed from. The on-disk store is redirected to
``tmp_path`` so no real ``config/pv_system.json`` is touched.

The read side is HTTP-200-always (an absent file is "not configured"); the
write side is strict and 400s per field — that asymmetry is the point.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _isolate_pv_config(monkeypatch, tmp_path):
    """Point the PV-system store at a per-test file and hand back its path."""
    import src.pv_system_config as pv_system_config

    path = tmp_path / "pv_system.json"
    monkeypatch.setattr(pv_system_config, "DEFAULT_CONFIG_PATH", path)
    return path


def test_get_unconfigured_is_200_not_500(client: TestClient) -> None:
    body = client.get("/api/energy/pv-system").json()
    assert body == {"configured": False, "arrays": [], "performance_ratio": 0.8}


def test_put_then_get_round_trips(client: TestClient) -> None:
    resp = client.put(
        "/api/energy/pv-system",
        json={
            "arrays": [
                {"kwp": 7.9, "tilt_deg": 15, "azimuth_deg": 0},
                {"kwp": 0.9, "tilt_deg": 15, "azimuth_deg": 180},
            ],
            "performance_ratio": 0.75,
        },
    )
    assert resp.status_code == 200
    out = resp.json()
    assert out["configured"] is True
    assert [a["kwp"] for a in out["arrays"]] == [7.9, 0.9]
    assert out["performance_ratio"] == 0.75
    assert out["total_kwp"] == 8.8

    reread = client.get("/api/energy/pv-system").json()
    assert reread["arrays"][1]["azimuth_deg"] == 180
    assert reread["performance_ratio"] == 0.75


def test_put_without_performance_ratio_keeps_the_stored_one(
    client: TestClient,
) -> None:
    """The shared dense-collection editor PUTs only its entry list, so an
    omitted derate must not silently reset to the default."""
    client.put(
        "/api/energy/pv-system",
        json={"arrays": [{"kwp": 5.0}], "performance_ratio": 0.72},
    )
    out = client.put(
        "/api/energy/pv-system",
        json={"arrays": [{"kwp": 5.0}, {"kwp": 1.0, "azimuth_deg": 90}]},
    ).json()
    assert out["performance_ratio"] == 0.72
    assert len(out["arrays"]) == 2


@pytest.mark.parametrize(
    "payload, fragment",
    [
        ({"arrays": []}, "at least one sub-array"),
        ({"arrays": [{"kwp": 0}]}, "kwp"),
        ({"arrays": [{"kwp": 1, "tilt_deg": -15}]}, "tilt_deg"),
        ({"arrays": [{"kwp": 1, "tilt_deg": 120}]}, "tilt_deg"),
        ({"arrays": [{"kwp": 1, "azimuth_deg": 400}]}, "azimuth_deg"),
        ({"arrays": [{"kwp": 1}], "performance_ratio": 0}, "performance_ratio"),
        ({"arrays": [{"kwp": 1}], "performance_ratio": 2}, "performance_ratio"),
    ],
)
def test_put_rejects_invalid_values_with_a_400(
    client: TestClient, payload: dict, fragment: str
) -> None:
    resp = client.put("/api/energy/pv-system", json=payload)
    assert resp.status_code == 400
    assert fragment in resp.json()["detail"]


def test_a_rejected_put_leaves_the_stored_config_untouched(
    client: TestClient, _isolate_pv_config
) -> None:
    client.put("/api/energy/pv-system", json={"arrays": [{"kwp": 5.0}]})
    client.put("/api/energy/pv-system", json={"arrays": [{"kwp": -3}]})
    assert client.get("/api/energy/pv-system").json()["arrays"] == [
        {"kwp": 5.0, "tilt_deg": 30.0, "azimuth_deg": 0.0}
    ]


def test_editing_a_legacy_flat_config_preserves_its_doc_note(
    client: TestClient, _isolate_pv_config
) -> None:
    """The real file on this host is still the pre-#555 flat shape and carries a
    hand-written ``_doc``; saving from the app must migrate without losing it."""
    _isolate_pv_config.write_text(
        json.dumps({"_doc": "why 8.8 kWp", "kwp": 8.8, "tilt_deg": 35}),
        encoding="utf-8",
    )
    assert client.get("/api/energy/pv-system").json()["arrays"] == [
        {"kwp": 8.8, "tilt_deg": 35.0, "azimuth_deg": 0.0}
    ]

    client.put(
        "/api/energy/pv-system",
        json={"arrays": [{"kwp": 8.8, "tilt_deg": 35}, {"kwp": 0.9, "azimuth_deg": 180}]},
    )
    raw = json.loads(_isolate_pv_config.read_text(encoding="utf-8"))
    assert raw["_doc"] == "why 8.8 kWp"
    assert "kwp" not in raw
    assert len(raw["arrays"]) == 2


# --------------------------- the panel-temperature switch's blast radius (#591)
# The editor has no control for the switch, but it does edit the ratio the
# switch reinterprets — so the two have to be validated together or the app
# itself becomes the way the inconsistent combination gets written.


def test_put_cannot_strand_an_armed_thermal_term_on_a_combined_ratio(
    client: TestClient, _isolate_pv_config
) -> None:
    """Lowering the derate back to a pre-#591 combined ~0.80 while the panel-
    temperature term is armed would double-count the thermal loss. It must be a
    400 naming the conflict, not a saved file."""
    _isolate_pv_config.write_text(
        json.dumps(
            {
                "arrays": [{"kwp": 8.0, "tilt_deg": 15}],
                "performance_ratio": 0.88,
                "thermal_model_enabled": True,
            }
        ),
        encoding="utf-8",
    )

    resp = client.put(
        "/api/energy/pv-system",
        json={"arrays": [{"kwp": 8.0, "tilt_deg": 15}], "performance_ratio": 0.8},
    )
    assert resp.status_code == 400
    assert "thermal_model_enabled" in resp.json()["detail"]

    raw = json.loads(_isolate_pv_config.read_text(encoding="utf-8"))
    assert raw["performance_ratio"] == 0.88


def test_an_ordinary_edit_leaves_the_hand_set_switch_armed(
    client: TestClient, _isolate_pv_config
) -> None:
    _isolate_pv_config.write_text(
        json.dumps(
            {
                "arrays": [{"kwp": 8.0}],
                "performance_ratio": 0.88,
                "thermal_model_enabled": True,
            }
        ),
        encoding="utf-8",
    )
    resp = client.put(
        "/api/energy/pv-system",
        json={"arrays": [{"kwp": 8.0}, {"kwp": 1.0, "azimuth_deg": 180}]},
    )
    assert resp.status_code == 200

    raw = json.loads(_isolate_pv_config.read_text(encoding="utf-8"))
    assert raw["thermal_model_enabled"] is True
    assert raw["performance_ratio"] == 0.88
