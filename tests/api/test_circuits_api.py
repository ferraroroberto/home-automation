"""API smoke for the per-circuit endpoints (issue #25).

The route handlers really run; only :func:`src.athom_client.fetch_circuits_state`
is monkeypatched, so no mDNS browse and no meter I/O ever happen here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.athom_client import CircuitReading, CircuitsState, MeterState

METER_ID = "AA:BB:CC:DD:EE:01"


def _state(reachable: bool = True) -> CircuitsState:
    """One meter, six channels: one live, one idle, four with no clamp."""
    channels = []
    for channel in range(1, 7):
        key = f"{METER_ID}:{channel}"
        if not reachable:
            channels.append(CircuitReading(channel=channel, key=key))
        elif channel == 1:
            channels.append(
                CircuitReading(
                    channel=channel, key=key, power_w=291.5, power_raw_w=-291.5,
                    current_a=1.81, energy_kwh=6.88, inverted=True,
                )
            )
        elif channel == 2:
            channels.append(
                CircuitReading(
                    channel=channel, key=key, power_w=0.0, power_raw_w=0.0,
                    current_a=0.0, energy_kwh=0.0,
                )
            )
        else:
            channels.append(CircuitReading(channel=channel, key=key))
    return CircuitsState(
        meters=[
            MeterState(
                meter_id=METER_ID,
                name="Athom Energy Monitor ddee01",
                model="China Athom Technology.Athom Energy Monitor(6 Channels)",
                host="192.0.2.73",
                reachable=reachable,
                error=None if reachable else "Offline — no response on the LAN.",
                voltage_v=239.4 if reachable else None,
                wifi_rssi_dbm=-68 if reachable else None,
                total_power_w=291.5 if reachable else None,
                channels=channels,
            )
        ],
        discovery_ok=True,
    )


@pytest.fixture
def _prefs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the prefs store at a temp file so tests never touch the real one."""
    import src.circuit_prefs as prefs

    path = tmp_path / "circuit_prefs.json"
    monkeypatch.setattr(prefs, "DEFAULT_PATH", path)
    return path


def _patch_state(monkeypatch: pytest.MonkeyPatch, state: CircuitsState) -> None:
    import app.webapp.routers.circuits as router

    async def fake() -> CircuitsState:
        return state

    monkeypatch.setattr(router, "fetch_circuits_state", fake)


def test_lists_every_channel_including_unclamped(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, _prefs: Path
) -> None:
    _patch_state(monkeypatch, _state())
    body = client.get("/api/circuits").json()

    assert body["discovery_ok"] is True
    assert len(body["meters"]) == 1
    channels = body["meters"][0]["channels"]
    assert [c["channel"] for c in channels] == [1, 2, 3, 4, 5, 6]
    # A measured 0 W and a never-measured channel must stay distinguishable.
    assert channels[1]["power_w"] == 0.0
    assert channels[2]["power_w"] is None


def test_reports_the_raw_and_corrected_power(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, _prefs: Path
) -> None:
    _patch_state(monkeypatch, _state())
    channel = client.get("/api/circuits").json()["meters"][0]["channels"][0]
    assert channel["power_raw_w"] == -291.5
    assert channel["power_w"] == 291.5
    assert channel["inverted"] is True


def test_an_offline_meter_keeps_its_channels(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, _prefs: Path
) -> None:
    _patch_state(monkeypatch, _state(reachable=False))
    meter = client.get("/api/circuits").json()["meters"][0]
    assert meter["reachable"] is False
    assert meter["error"]
    assert len(meter["channels"]) == 6
    assert all(c["power_w"] is None for c in meter["channels"])


def test_rename_a_channel_then_read_it_back(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, _prefs: Path
) -> None:
    _patch_state(monkeypatch, _state())
    key = f"{METER_ID}:1"
    saved = client.put(
        f"/api/circuits/{key}/display_name", json={"display_name": "water heater"}
    )
    assert saved.status_code == 200
    assert saved.json() == {"key": key, "display_name": "water heater"}

    channel = client.get("/api/circuits").json()["meters"][0]["channels"][0]
    assert channel["display_name"] == "water heater"


def test_a_meter_is_renamed_through_the_same_endpoint(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, _prefs: Path
) -> None:
    _patch_state(monkeypatch, _state())
    client.put(
        f"/api/circuits/{METER_ID}/display_name", json={"display_name": "cuadro principal"}
    )
    meter = client.get("/api/circuits").json()["meters"][0]
    assert meter["display_name"] == "cuadro principal"
    # Renaming the meter must not leak onto its channels.
    assert meter["channels"][0]["display_name"] is None


def test_invert_is_persisted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, _prefs: Path
) -> None:
    from src.circuit_prefs import load_inverted_channels

    _patch_state(monkeypatch, _state())
    key = f"{METER_ID}:3"
    response = client.put(f"/api/circuits/{key}/invert", json={"invert": True})
    assert response.status_code == 200
    assert response.json() == {"key": key, "invert": True}
    assert load_inverted_channels(_prefs) == {key: True}


def test_invert_rejects_a_non_boolean(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, _prefs: Path
) -> None:
    _patch_state(monkeypatch, _state())
    response = client.put(f"/api/circuits/{METER_ID}:1/invert", json={"invert": "yes please"})
    assert response.status_code == 422


def test_invert_does_not_re_run_discovery(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, _prefs: Path
) -> None:
    """Regression: clearing discovery here could blank the card (see #25).

    A sweep can legitimately come back empty, and with the cache just cleared
    there is no previous list to fall back on — so the flip must only drop the
    per-meter read cache.
    """
    import app.webapp.routers.circuits as router
    import src.athom_client as ac

    _patch_state(monkeypatch, _state())
    cleared: list[str] = []
    monkeypatch.setattr(router, "clear_caches", lambda: cleared.append("all"))
    monkeypatch.setattr(router, "clear_read_cache", lambda: cleared.append("read"))

    client.put(f"/api/circuits/{METER_ID}:1/invert", json={"invert": True})
    assert cleared == ["read"]
    assert ac.clear_read_cache is not ac.clear_caches
