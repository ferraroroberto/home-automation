"""API smoke for the fleet solar-boost coordinator knobs (issue #562).

``GET/PUT /api/hvac/boost-coordinator`` round-trips the sequencing knobs the
Energy tab's "Solar boost" card edits. The on-disk store is redirected to
``tmp_path`` so no real ``config/hvac_boost.json`` is touched.

Same deliberate asymmetry as the PV-system editor: the read side is
HTTP-200-always (a broken file must not stop the engine), while the write side is
strict and 400s naming the field — most importantly for a settle interval under
the 5-minute floor, which is a physical constraint of the solar meter's publish
cadence rather than a matter of taste.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _isolate_boost_config(monkeypatch, tmp_path):
    """Point the coordinator store at a per-test file and hand back its path."""
    import src.hvac_automation as hvac_automation

    path = tmp_path / "hvac_boost.json"
    monkeypatch.setattr(hvac_automation, "BOOST_CONFIG_PATH", path)
    return path


def test_get_unconfigured_returns_the_defaults_not_a_500(client: TestClient) -> None:
    body = client.get("/api/hvac/boost-coordinator").json()
    assert body["settle_interval_s"] == 300
    assert body["admission_margin_w"] == 0.0
    assert body["hard_deficit_w"] == 1000.0
    assert body["ordering_policy"] == "stable"
    # The editor renders its input bounds from the server, not a copied constant.
    assert body["min_settle_interval_s"] == 300
    assert body["ordering_policies"] == ["stable"]


def test_put_then_get_round_trips(client: TestClient) -> None:
    resp = client.put(
        "/api/hvac/boost-coordinator",
        json={
            "settle_interval_s": 600,
            "admission_margin_w": 400,
            "hard_deficit_w": 1500,
            "ordering_policy": "stable",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["settle_interval_s"] == 600

    reread = client.get("/api/hvac/boost-coordinator").json()
    assert reread["settle_interval_s"] == 600
    assert reread["admission_margin_w"] == 400
    assert reread["hard_deficit_w"] == 1500


def test_put_of_one_field_keeps_the_others(client: TestClient) -> None:
    """The card saves a row on blur, so a single-field PUT must not reset the
    knobs the user did not touch."""
    client.put("/api/hvac/boost-coordinator", json={"settle_interval_s": 900})
    out = client.put(
        "/api/hvac/boost-coordinator", json={"admission_margin_w": 250}
    ).json()
    assert out["settle_interval_s"] == 900
    assert out["admission_margin_w"] == 250


@pytest.mark.parametrize(
    "payload, fragment",
    [
        ({"settle_interval_s": 60}, "settle_interval_s"),
        ({"settle_interval_s": 299}, "settle_interval_s"),
        ({"settle_interval_s": 7200}, "settle_interval_s"),
        ({"admission_margin_w": -1}, "admission_margin_w"),
        ({"hard_deficit_w": -250}, "hard_deficit_w"),
        ({"ordering_policy": "round-robin"}, "ordering_policy"),
    ],
)
def test_put_rejects_invalid_values_with_a_400(
    client: TestClient, payload: dict, fragment: str
) -> None:
    resp = client.put("/api/hvac/boost-coordinator", json=payload)
    assert resp.status_code == 400
    assert fragment in resp.json()["detail"]


def test_the_settle_floor_rejection_explains_the_physical_reason(
    client: TestClient,
) -> None:
    """A user who types 2 minutes needs to know why it was refused, not just
    that it was — the floor tracks the solar meter's publish grid."""
    detail = client.put(
        "/api/hvac/boost-coordinator", json={"settle_interval_s": 120}
    ).json()["detail"]
    assert "5-minute" in detail


def test_a_rejected_put_leaves_the_stored_config_untouched(
    client: TestClient,
) -> None:
    client.put("/api/hvac/boost-coordinator", json={"settle_interval_s": 600})
    client.put("/api/hvac/boost-coordinator", json={"settle_interval_s": 30})
    assert client.get("/api/hvac/boost-coordinator").json()["settle_interval_s"] == 600


def test_a_hand_edited_file_below_the_floor_reads_back_clamped(
    client: TestClient, _isolate_boost_config
) -> None:
    """The read path never fails on a hand-broken file; it clamps and serves."""
    _isolate_boost_config.write_text(
        json.dumps({"settle_interval_s": 45, "ordering_policy": "nonsense"}),
        encoding="utf-8",
    )
    body = client.get("/api/hvac/boost-coordinator").json()
    assert body["settle_interval_s"] == 300
    assert body["ordering_policy"] == "stable"


def test_saving_from_the_app_preserves_a_hand_written_doc_note(
    client: TestClient, _isolate_boost_config
) -> None:
    _isolate_boost_config.write_text(
        json.dumps({"_doc": "10 min because the compressor ramps slowly"}),
        encoding="utf-8",
    )
    client.put("/api/hvac/boost-coordinator", json={"settle_interval_s": 600})
    raw = json.loads(_isolate_boost_config.read_text(encoding="utf-8"))
    assert raw["_doc"] == "10 min because the compressor ramps slowly"
    assert raw["settle_interval_s"] == 600
