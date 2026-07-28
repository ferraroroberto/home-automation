"""Wi-Fi walk-test API tests (issue #547).

Drives the real ``/api/network/survey`` routes with the AP/router lookup
monkeypatched, so nothing touches the live NETGEAR or ZTE. The cases that matter:
the server resolves the telemetry itself rather than trusting the client, a MAC
on neither radio is recorded as its own state instead of a silent null, deletes
accept a free-form room label in the body, and the throughput payload honours
both its size and its cap.
"""

from __future__ import annotations

from typing import Optional

import pytest
from fastapi.testclient import TestClient

from src.network_client import NetDevice


@pytest.fixture(autouse=True)
def _isolate_survey_db(tmp_path, monkeypatch) -> None:
    """Point the survey store at a per-test temp DB, never the real one."""
    import src.network_survey as ns

    monkeypatch.setattr(ns, "DEFAULT_DB_PATH", tmp_path / "network_survey.sqlite3")


def _patch_lookup(
    monkeypatch: pytest.MonkeyPatch,
    device: Optional[NetDevice],
    sources_read: int = 2,
) -> list:
    """Stub the AP/router lookup; returns the list of MACs it was asked about.

    ``sources_read`` mirrors the real signature: how many of the two boxes
    actually answered. It is what separates a dead zone from an unusable probe.
    """
    import app.webapp.routers.network as router_mod

    seen: list = []

    async def _fake(mac: str):
        seen.append(mac)
        return device, sources_read

    monkeypatch.setattr(router_mod, "resolve_wireless_client_by_mac", _fake)
    return seen


def _device(**kw) -> NetDevice:
    base = dict(
        mac="AA:BB:CC:00:00:01",
        ip="192.0.2.20",
        name="phone",
        conn_type="5GHz",
        signal=72,
        link_rate=390,
        ssid="TestNet",
        source="ap",
    )
    base.update(kw)
    return NetDevice(**base)


def test_post_records_server_resolved_telemetry(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _patch_lookup(monkeypatch, _device())

    res = client.post(
        "/api/network/survey",
        json={
            "room": "Kitchen",
            "mac": "aa:bb:cc:00:00:01",
            "rtt_ms": 14.2,
            "jitter_ms": 2.0,
            "loss_pct": 0.0,
            "throughput_mbps": 210.5,
        },
    )
    assert res.status_code == 200
    body = res.json()
    # The signal/band/ssid came from the server's own lookup, not the request.
    assert seen == ["aa:bb:cc:00:00:01"]
    assert body["signal"] == 72
    assert body["band"] == "5GHz"
    assert body["ssid"] == "TestNet"
    assert body["source"] == "ap"
    assert body["found"] is True
    # The browser-measured legs ride along unchanged.
    assert body["rtt_ms"] == 14.2
    assert body["throughput_mbps"] == 210.5


def test_router_radio_association_is_attributed_to_the_router(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Which box heard the phone is the load-bearing column for AP placement."""
    _patch_lookup(monkeypatch, _device(source="router", conn_type="2.4GHz", signal=38))

    body = client.post(
        "/api/network/survey", json={"room": "Attic", "mac": "AA:BB:CC:00:00:01"}
    ).json()
    assert body["source"] == "router"
    assert body["band"] == "2.4GHz"
    assert body["signal"] == 38


def test_device_on_neither_radio_is_recorded_as_not_found(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead spot is a result, not a failure — and never a bare null."""
    _patch_lookup(monkeypatch, None)

    res = client.post(
        "/api/network/survey", json={"room": "Cellar", "mac": "AA:BB:CC:00:00:01"}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["signal"] is None
    assert body["source"] == "not_found"
    assert body["found"] is False

    summary = client.get("/api/network/survey").json()["rooms"][0]
    assert summary["room"] == "Cellar"
    assert summary["last_found"] is False


@pytest.mark.parametrize("sources_read", [0, 1])
def test_unreadable_sources_record_unknown_not_a_dead_zone(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sources_read: int
) -> None:
    """An AP that timed out must not be reported as "no coverage here".

    The client could have been associated to exactly the box that stayed silent,
    so the sample establishes nothing — recording it as `not_found` would invent
    a coverage claim out of an outage.
    """
    _patch_lookup(monkeypatch, None, sources_read=sources_read)

    body = client.post(
        "/api/network/survey", json={"room": "Loft", "mac": "AA:BB:CC:00:00:01"}
    ).json()
    assert body["source"] == "unknown"
    assert body["source"] != "not_found"
    assert body["signal"] is None
    assert body["found"] is False


def test_unknown_room_does_not_head_the_coverage_report(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_lookup(monkeypatch, _device(signal=55))
    client.post("/api/network/survey", json={"room": "Office", "mac": "AA:BB:CC:00:00:01"})
    _patch_lookup(monkeypatch, None, sources_read=2)
    client.post("/api/network/survey", json={"room": "Attic", "mac": "AA:BB:CC:00:00:01"})
    _patch_lookup(monkeypatch, None, sources_read=0)
    client.post("/api/network/survey", json={"room": "Loft", "mac": "AA:BB:CC:00:00:01"})

    rooms = client.get("/api/network/survey").json()["rooms"]
    # Real dead zone first, then the measured room, then the one that measured
    # nothing at all.
    assert [r["room"] for r in rooms] == ["Attic", "Office", "Loft"]


@pytest.mark.parametrize("payload", [{"room": "  ", "mac": "AA:BB"}, {"room": "Hall", "mac": ""}])
def test_post_rejects_a_sample_with_no_room_or_no_subject(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, payload: dict
) -> None:
    _patch_lookup(monkeypatch, _device())
    assert client.post("/api/network/survey", json=payload).status_code == 400


def test_get_returns_rooms_samples_and_known_labels(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_lookup(monkeypatch, _device(signal=80))
    client.post("/api/network/survey", json={"room": "Office", "mac": "AA:BB:CC:00:00:01"})
    _patch_lookup(monkeypatch, _device(signal=35))
    client.post("/api/network/survey", json={"room": "Attic", "mac": "AA:BB:CC:00:00:01"})

    body = client.get("/api/network/survey").json()
    assert len(body["samples"]) == 2
    assert body["known_rooms"] == ["Attic", "Office"]
    # Weakest room first — the walk test's whole reason for existing.
    assert [r["room"] for r in body["rooms"]] == ["Attic", "Office"]


def test_delete_accepts_a_room_label_containing_a_slash(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Room labels are free-form text, which is why deletes are body-carried."""
    _patch_lookup(monkeypatch, _device())
    client.post(
        "/api/network/survey", json={"room": "Garage / Cellar", "mac": "AA:BB:CC:00:00:01"}
    )

    res = client.post("/api/network/survey/delete", json={"room": "Garage / Cellar"})
    assert res.status_code == 200
    assert res.json()["deleted"] == 1
    assert client.get("/api/network/survey").json()["rooms"] == []


def test_delete_by_sample_id_removes_one_row(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_lookup(monkeypatch, _device())
    first = client.post(
        "/api/network/survey", json={"room": "Kitchen", "mac": "AA:BB:CC:00:00:01"}
    ).json()
    client.post("/api/network/survey", json={"room": "Kitchen", "mac": "AA:BB:CC:00:00:01"})

    assert client.post(
        "/api/network/survey/delete", json={"sample_id": first["id"]}
    ).json()["deleted"] == 1
    assert len(client.get("/api/network/survey").json()["samples"]) == 1


def test_delete_without_a_target_is_a_400(client: TestClient) -> None:
    assert client.post("/api/network/survey/delete", json={}).status_code == 400


def test_payload_returns_the_requested_size_uncached(client: TestClient) -> None:
    res = client.get("/api/network/survey/payload?bytes=4096")
    assert res.status_code == 200
    assert len(res.content) == 4096
    # Without no-store a second probe in the same room would time the cache.
    assert "no-store" in res.headers["cache-control"]


def test_payload_size_is_capped_and_floored(client: TestClient) -> None:
    """The body is generated in RAM per request, so the cap is not advisory."""
    assert client.get("/api/network/survey/payload?bytes=99999999").status_code == 422
    assert client.get("/api/network/survey/payload?bytes=1").status_code == 422


def test_payload_default_size_needs_no_query(client: TestClient) -> None:
    res = client.get("/api/network/survey/payload")
    assert res.status_code == 200
    assert len(res.content) == 2 * 1024 * 1024
