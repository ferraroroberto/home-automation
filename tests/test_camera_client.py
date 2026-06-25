"""Unit tests for :mod:`src.camera_client` config + MAC address-recovery (#190).

Covers ``mac`` parsing, the atomic host-rewrite that persists a rediscovered
address, and the ``_recover_host`` decision logic. No ONVIF, no network — the
rediscovery resolver is monkeypatched.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from src import camera_client as C
from src import network_client


def _write(path: Path, cams: list) -> None:
    path.write_text(json.dumps(cams, indent=2), encoding="utf-8")


def test_load_cameras_parses_optional_mac(tmp_path: Path) -> None:
    path = tmp_path / "cameras.json"
    _write(path, [
        {"id": "garden", "host": "192.168.0.23", "mac": "b0:6b:11:f8:37:7f",
         "username": "admin", "password": "x"},
        {"id": "attic", "host": "192.168.0.24", "username": "admin", "password": "y"},
    ])
    cams = C.load_cameras(path)
    assert cams[0].mac == "b0:6b:11:f8:37:7f"
    assert cams[1].mac is None  # absent → None, not an error


def test_persist_camera_host_rewrites_only_target_preserving_rest(tmp_path: Path) -> None:
    path = tmp_path / "cameras.json"
    _write(path, [
        {"id": "garden", "host": "192.168.0.89", "username": "admin", "password": "secret"},
        {"id": "attic", "host": "192.168.0.24", "username": "admin", "password": "z"},
    ])
    C._persist_camera_host("garden", "192.168.0.23", path=path)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk[0]["host"] == "192.168.0.23"
    assert on_disk[0]["password"] == "secret"   # other fields untouched
    assert on_disk[1]["host"] == "192.168.0.24"  # other cameras untouched
    assert not (tmp_path / "cameras.json.tmp").exists()  # atomic sidecar cleaned


def test_recover_host_rediscovers_and_persists(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "cameras.json"
    _write(path, [{"id": "garden", "host": "192.168.0.89", "username": "a", "password": "b"}])

    async def fake_resolve(mac: str):
        return "192.168.0.23"

    monkeypatch.setattr(network_client, "resolve_ip_by_mac", fake_resolve)
    monkeypatch.setattr(C, "_CONFIG_PATH", path)

    cfg = C.CameraConfig(id="garden", host="192.168.0.89", username="a", password="b",
                         mac="b0:6b:11:f8:37:7f")
    recovered = asyncio.run(C._recover_host(cfg))
    assert recovered is not None and recovered.host == "192.168.0.23"
    # The recovered address is written back to disk for future loads.
    assert json.loads(path.read_text(encoding="utf-8"))[0]["host"] == "192.168.0.23"


def test_recover_host_noop_without_mac_or_when_unchanged(monkeypatch) -> None:
    async def fake_resolve(mac: str):
        return "192.168.0.89"  # same as configured host

    monkeypatch.setattr(network_client, "resolve_ip_by_mac", fake_resolve)
    no_mac = C.CameraConfig(id="g", host="192.168.0.89", username="a", password="b")
    same = C.CameraConfig(id="g", host="192.168.0.89", username="a", password="b",
                          mac="b0:6b:11:f8:37:7f")
    assert asyncio.run(C._recover_host(no_mac)) is None     # no MAC → no recovery
    assert asyncio.run(C._recover_host(same)) is None       # unchanged IP → no-op
