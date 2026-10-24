"""Route table behind the ``mock_network`` fixture, one handler per API area.

Each handler takes the request and returns True once it has fulfilled it, so
``handle_network`` can walk ``ROUTE_TABLE`` in order and fall through to the
snapshot when none claims the request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List

from playwright.sync_api import Route

from tests.e2e.fixtures._routes import (
    device_mac,
    json_body,
    patch_field,
    text_or_none,
)


@dataclass
class NetworkMock:
    """The state one ``mock_network`` install serves and mutates."""

    body: Dict
    history: Dict
    failures_before_success: int = 0
    attempts: int = 0
    nightly_on: bool = False
    survey: Dict = field(
        default_factory=lambda: {"rooms": [], "samples": [], "known_rooms": []}
    )
    survey_seq: int = 0


def _fulfill_json(route: Route, body, status: int = 200) -> None:
    route.fulfill(status=status, content_type="application/json", body=json_body(body))


def _survey_recompute(mock: NetworkMock) -> None:
    survey = mock.survey
    rooms: Dict[str, Dict] = {}
    for sample in survey["samples"]:  # newest first
        entry = rooms.setdefault(sample["room"], {
            "room": sample["room"],
            "count": 0,
            "last_recorded_at": sample["recorded_at"],
            "last_signal": sample["signal"],
            "last_band": sample["band"],
            "last_ssid": sample["ssid"],
            "last_source": sample["source"],
            "last_found": sample["found"],
            "last_link_rate": sample["link_rate"],
            "last_rtt_ms": sample["rtt_ms"],
            "last_throughput_mbps": sample["throughput_mbps"],
            "best_signal": None,
            "worst_signal": None,
        })
        entry["count"] += 1
        if sample["signal"] is not None:
            best, worst = entry["best_signal"], entry["worst_signal"]
            entry["best_signal"] = (
                sample["signal"] if best is None else max(best, sample["signal"])
            )
            entry["worst_signal"] = (
                sample["signal"] if worst is None else min(worst, sample["signal"])
            )
    survey["rooms"] = sorted(
        rooms.values(),
        key=lambda e: (
            0 if e["last_signal"] is None else 1,
            e["last_signal"] if e["last_signal"] is not None else 0,
            e["room"],
        ),
    )
    survey["known_rooms"] = sorted(rooms)


def _internet_history(mock: NetworkMock, route: Route, url: str, method: str) -> bool:
    # The tile's sparkline series (#840). Answered before the failure
    # counter so it never eats into ``failures_before_success``, which
    # counts reads of the LAN snapshot itself.
    if "/api/network/internet-history" not in url:
        return False
    _fulfill_json(route, mock.history)
    return True


def _speedtest_prefs(mock: NetworkMock, route: Route, url: str, method: str) -> bool:
    # The opt-in nightly speed test (#840): off until a PUT turns it on.
    if "/api/network/speedtest-prefs" not in url:
        return False
    if method == "PUT":
        mock.nightly_on = bool((route.request.post_data_json or {}).get("nightly_enabled"))
    _fulfill_json(route, {
        "prefs": {"nightly_enabled": mock.nightly_on},
        "telegram_configured": False,
    })
    return True


def _failure_gate(mock: NetworkMock, route: Route, url: str, method: str) -> bool:
    mock.attempts += 1
    if mock.attempts > mock.failures_before_success:
        return False
    _fulfill_json(route, {"detail": "Temporary network read failure"}, status=503)
    return True


def _survey_delete(mock: NetworkMock, route: Route, body_json: Dict) -> None:
    samples = mock.survey["samples"]
    room = (body_json.get("room") or "").strip()
    sample_id = body_json.get("sample_id")
    before = len(samples)
    mock.survey["samples"] = [
        s for s in samples
        if not (room and s["room"] == room)
        and not (sample_id is not None and s["id"] == sample_id)
    ]
    _survey_recompute(mock)
    _fulfill_json(route, {"deleted": before - len(mock.survey["samples"])})


def _survey_record(mock: NetworkMock, route: Route, body_json: Dict) -> None:
    # POST /api/network/survey — the server resolves the telemetry,
    # so the stub supplies it from the fixture device list by MAC.
    mac = (body_json.get("mac") or "").strip().upper()
    device = next(
        (d for d in mock.body["devices"] if d["mac"].upper() == mac), None
    )
    mock.survey_seq += 1
    sample = {
        "id": mock.survey_seq,
        "recorded_at": 1_700_000_000 + mock.survey_seq,
        "room": (body_json.get("room") or "").strip(),
        "mac": mac,
        "signal": device["signal"] if device else None,
        "link_rate": device["link_rate"] if device else None,
        "band": device["conn_type"] if device else None,
        "ssid": device["ssid"] if device else None,
        "source": device["source"] if device else "not_found",
        "found": device is not None,
        "rtt_ms": body_json.get("rtt_ms"),
        "jitter_ms": body_json.get("jitter_ms"),
        "loss_pct": body_json.get("loss_pct"),
        "throughput_mbps": body_json.get("throughput_mbps"),
    }
    mock.survey["samples"].insert(0, sample)
    _survey_recompute(mock)
    _fulfill_json(route, sample)


def _survey(mock: NetworkMock, route: Route, url: str, method: str) -> bool:
    # The route glob is broad enough to capture /api/network/survey*, so this
    # fixture owns those responses too — otherwise the survey card would be
    # served the LAN snapshot and read an empty survey out of a shape that does
    # not have one (Wi-Fi walk test, issue #547).
    if "/api/network/survey" not in url:
        return False
    if "/survey/payload" in url:
        route.fulfill(
            status=200,
            content_type="application/octet-stream",
            body="x" * 4096,
        )
    elif method == "GET":
        _fulfill_json(route, mock.survey)
    elif url.endswith("/survey/delete"):
        _survey_delete(mock, route, route.request.post_data_json or {})
    else:
        _survey_record(mock, route, route.request.post_data_json or {})
    return True


def _group_rename(mock: NetworkMock, route: Route, body_json: Dict) -> None:
    name = (body_json.get("name") or "").strip()
    new_name = (body_json.get("new_name") or "").strip()
    moved = 0
    for device in mock.body["devices"]:
        if (device.get("group") or "") == name:
            device["group"] = new_name
            moved += 1
    _fulfill_json(route, {"name": name, "new_name": new_name, "moved": moved})


def _group_delete(mock: NetworkMock, route: Route, body_json: Dict) -> None:
    name = (body_json.get("name") or "").strip()
    moved = 0
    for device in mock.body["devices"]:
        if (device.get("group") or "") == name:
            device["group"] = None
            moved += 1
    _fulfill_json(route, {"name": name, "moved": moved})


def _device_field_write(mock: NetworkMock, route: Route, url: str, body_json: Dict) -> bool:
    if "/api/network/devices/" not in url:
        return False
    fields = {
        "/display_name": ("display_name", text_or_none(body_json.get("display_name"))),
        "/hidden": ("hidden", bool(body_json.get("hidden"))),
        "/group": ("group", text_or_none(body_json.get("group"))),
    }
    for suffix, (name, value) in fields.items():
        if url.endswith(suffix):
            patch_field(route, mock.body["devices"], "mac", device_mac(url), name, value)
            return True
    return False


def _wifi_field_write(mock: NetworkMock, route: Route, url: str, body_json: Dict) -> bool:
    fields = {
        "/api/network/wifi/display_name": (
            "display_name", text_or_none(body_json.get("display_name"))
        ),
        "/api/network/wifi/hidden": ("hidden", bool(body_json.get("hidden"))),
    }
    for suffix, (name, value) in fields.items():
        if url.endswith(suffix):
            patch_field(
                route, mock.body["wifi"]["bssids"], "wifi_id",
                body_json.get("wifi_id"), name, value,
            )
            return True
    return False


def _writes(mock: NetworkMock, route: Route, url: str, method: str) -> bool:
    if method not in {"PUT", "POST"}:
        return False
    body_json = route.request.post_data_json or {}
    if _device_field_write(mock, route, url, body_json):
        return True
    if url.endswith("/api/network/groups/rename"):
        _group_rename(mock, route, body_json)
        return True
    if url.endswith("/api/network/groups/delete"):
        _group_delete(mock, route, body_json)
        return True
    return _wifi_field_write(mock, route, url, body_json)


Handler = Callable[[NetworkMock, Route, str, str], bool]

# Order matters: the history and prefs routes answer before the failure gate so
# they never spend a ``failures_before_success`` attempt, and the survey and
# write routes only see requests that gate let through.
ROUTE_TABLE: List[Handler] = [
    _internet_history,
    _speedtest_prefs,
    _failure_gate,
    _survey,
    _writes,
]


def handle_network(mock: NetworkMock, route: Route) -> None:
    url = route.request.url
    method = route.request.method.upper()
    for handler in ROUTE_TABLE:
        if handler(mock, route, url, method):
            return
    _fulfill_json(route, mock.body)
