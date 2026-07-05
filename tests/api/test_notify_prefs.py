"""API smoke for the automatic-alarm notification toggles.

``GET/PUT /api/security/notify-prefs`` round-trips the five booleans and reports
whether Telegram is configured. The on-disk store is redirected to ``tmp_path``
and the "configured" probe is faked — no real config, no Telegram.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_notify_prefs_round_trip_and_configured_flag(
    client: TestClient, monkeypatch, tmp_path
) -> None:
    import src.alarm_notify_prefs as prefs_mod

    store = tmp_path / "alarm_notify_prefs.json"
    monkeypatch.setattr(prefs_mod, "DEFAULT_PATH", store)
    monkeypatch.setattr(
        "app.webapp.routers.security_notify.is_notify_configured", lambda: True
    )

    # Default state: only the error toggle is on.
    body = client.get("/api/security/notify-prefs").json()
    assert body["prefs"]["error"] is True
    assert body["prefs"]["schedule_arm"] is False
    assert body["telegram_configured"] is True

    # Toggling persists and is reflected on the next GET.
    resp = client.put(
        "/api/security/notify-prefs",
        json={"schedule_arm": True, "error": False},
    )
    assert resp.status_code == 200
    assert resp.json()["prefs"]["schedule_arm"] is True
    assert resp.json()["prefs"]["error"] is False

    reread = client.get("/api/security/notify-prefs").json()["prefs"]
    assert reread["schedule_arm"] is True
    assert reread["error"] is False
    # Untouched keys keep their default.
    assert reread["presence_disarm"] is False


def test_notify_prefs_unconfigured_reports_false(
    client: TestClient, monkeypatch, tmp_path
) -> None:
    import src.alarm_notify_prefs as prefs_mod

    monkeypatch.setattr(prefs_mod, "DEFAULT_PATH", tmp_path / "p.json")
    monkeypatch.setattr(
        "app.webapp.routers.security_notify.is_notify_configured", lambda: False
    )
    assert client.get("/api/security/notify-prefs").json()["telegram_configured"] is False


def test_power_notify_prefs_round_trip(client: TestClient, monkeypatch, tmp_path) -> None:
    import src.power_notify_prefs as prefs_mod

    monkeypatch.setattr(prefs_mod, "DEFAULT_PATH", tmp_path / "power_notify_prefs.json")
    monkeypatch.setattr("app.webapp.routers.ups.is_notify_configured", lambda: True)

    # Default: all three power toggles on.
    body = client.get("/api/ups/notify-prefs").json()
    assert body["prefs"] == {
        "power_lost": True,
        "power_restored": True,
        "auto_shutdown_low_battery": True,
    }
    assert body["telegram_configured"] is True

    resp = client.put(
        "/api/ups/notify-prefs", json={"power_restored": False, "auto_shutdown_low_battery": False}
    )
    assert resp.status_code == 200
    assert resp.json()["prefs"] == {
        "power_lost": True,
        "power_restored": False,
        "auto_shutdown_low_battery": False,
    }

    reread = client.get("/api/ups/notify-prefs").json()["prefs"]
    assert reread["power_restored"] is False
    assert reread["auto_shutdown_low_battery"] is False
