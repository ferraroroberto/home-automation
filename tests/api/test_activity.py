"""API smoke for the unified activity log endpoint (#289).

Drives the real ``GET /api/activity`` over the in-process app, seeding events
into the per-test temp telemetry DB (the ``_isolate_telemetry`` fixture) and
asserting the server-side filtering.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src import telemetry


def _seed() -> None:
    telemetry.record_event("alarm", "arm", source="manual", outcome="ok", ts=1_700_000_000)
    telemetry.record_event("power", "power_lost", severity="warning", ts=1_700_000_100)
    telemetry.record_event("plug", "plug_on", entity_id="dev-1", source="manual", ts=1_700_000_200)


def test_activity_empty_ok(client: TestClient) -> None:
    resp = client.get("/api/activity")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"events": [], "count": 0}


def test_activity_returns_events_newest_first(client: TestClient) -> None:
    _seed()
    resp = client.get("/api/activity")
    assert resp.status_code == 200
    events = resp.json()["events"]
    assert [e["event_type"] for e in events] == ["plug_on", "power_lost", "arm"]


def test_activity_filters_by_domain(client: TestClient) -> None:
    _seed()
    resp = client.get("/api/activity", params={"domain": "power"})
    events = resp.json()["events"]
    assert len(events) == 1
    assert events[0]["domain"] == "power"
    assert events[0]["severity"] == "warning"


def test_activity_filters_by_type_and_limit(client: TestClient) -> None:
    _seed()
    by_type = client.get("/api/activity", params={"type": "plug_on"}).json()["events"]
    assert len(by_type) == 1 and by_type[0]["entity_id"] == "dev-1"

    capped = client.get("/api/activity", params={"limit": 1}).json()["events"]
    assert len(capped) == 1


def test_activity_domains_lists_present_domains(client: TestClient) -> None:
    _seed()
    resp = client.get("/api/activity/domains")
    assert resp.status_code == 200
    assert resp.json()["domains"] == ["alarm", "plug", "power"]


def test_activity_readings_empty_ok(client: TestClient) -> None:
    resp = client.get("/api/activity/readings")
    assert resp.status_code == 200
    assert resp.json() == {"readings": [], "count": 0}


def test_activity_readings_filters_by_domain_and_metric(client: TestClient) -> None:
    from src.telemetry import Reading

    telemetry.record_readings(
        [Reading("hvac", "u1", "room_temperature", value_num=21.0, unit="degC")],
        ts=1_700_000_000,
    )
    telemetry.record_readings(
        [Reading("plug", "d1", "power_w", value_num=55.0, unit="W")],
        ts=1_700_000_100,
    )
    all_rows = client.get("/api/activity/readings").json()["readings"]
    assert {r["domain"] for r in all_rows} == {"hvac", "plug"}

    hvac = client.get("/api/activity/readings", params={"domain": "hvac"}).json()["readings"]
    assert len(hvac) == 1 and hvac[0]["metric"] == "room_temperature"

    by_metric = client.get("/api/activity/readings", params={"metric": "power_w"}).json()["readings"]
    assert len(by_metric) == 1 and by_metric[0]["value_num"] == 55.0
