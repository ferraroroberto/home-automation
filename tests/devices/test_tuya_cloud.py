"""Unit tests for the Tuya Cloud bootstrap merge (issue #612).

The point of ``src.tuya_cloud`` is that it does **not** persist TinyTuya's
``getdevices()`` return value directly. That value is lossy: rows TinyTuya
considers "changed" are rebuilt by its ``filter_devices()`` from a field
whitelist with no ``ip`` and no ``dps``, so writing it straight out drops the
stored LAN address of any device whose cloud metadata shifted. Measured
against this repo's real ``devices.json``, that was 4 of the 7 devices that
had an address — each of which would then read as offline until the next
broadcast scan.

These tests pin that invariant plus the credential/authorization failures,
with no network access anywhere.
"""

from __future__ import annotations

import json

import pytest

from src import tuya_client, tuya_cloud
from src.tuya_cloud import TuyaCloudError, _merge_cloud_rows, sync_devices_from_cloud


def _existing_row() -> dict:
    """A devices.json row as it looks after a wizard run plus a LAN snapshot."""
    return {
        "id": "dev-1",
        "name": "Old name",
        "key": "old-key",
        "ip": "192.168.0.50",
        "version": 3.3,
        "mapping": {"1": {"code": "switch_1"}},
        "dps": {"1": True, "19": 42},
    }


# ------------------------------------------------------------------ merging
def test_merge_preserves_lan_fields_when_cloud_metadata_changes() -> None:
    """The regression this module exists for: a changed row keeps its address.

    The cloud row carries a rotated local key and no ``ip``/``version``/``dps``
    at all — exactly the shape ``filter_devices()`` produces.
    """
    entries = [_existing_row()]

    added, updated = _merge_cloud_rows(
        entries,
        [{"id": "dev-1", "name": "New name", "key": "new-key", "category": "cz"}],
    )

    assert added == []
    assert updated == ["dev-1"]
    row = entries[0]
    # Cloud is authoritative for identity/capability…
    assert row["key"] == "new-key"
    assert row["name"] == "New name"
    assert row["category"] == "cz"
    # …and must not have touched anything only the LAN knows.
    assert row["ip"] == "192.168.0.50"
    assert row["version"] == 3.3
    assert row["dps"] == {"1": True, "19": 42}
    assert row["mapping"] == {"1": {"code": "switch_1"}}


def test_merge_never_takes_local_fields_from_a_cloud_row() -> None:
    """Even a cloud row that *does* carry ip/version must not overwrite ours.

    ``last_ip`` in the Tuya payload is the account's WAN address, so a cloud
    row's idea of "ip" is never the LAN address this app dials.
    """
    entries = [_existing_row()]

    _merge_cloud_rows(entries, [{"id": "dev-1", "ip": "83.42.102.25", "version": 3.1}])

    assert entries[0]["ip"] == "192.168.0.50"
    assert entries[0]["version"] == 3.3


def test_cloud_owned_and_local_only_fields_stay_disjoint() -> None:
    """The whitelist is the whole guard — nothing local may leak into it."""
    overlap = set(tuya_cloud._CLOUD_OWNED_FIELDS) & set(tuya_cloud._LOCAL_ONLY_FIELDS)

    assert not overlap, f"the cloud must never own {sorted(overlap)}"


def test_merge_appends_a_newly_paired_device() -> None:
    entries = [_existing_row()]

    added, updated = _merge_cloud_rows(
        entries,
        [
            {"id": "dev-1", "name": "Old name", "key": "old-key"},
            {"id": "dev-2", "name": "New plug", "key": "k2", "category": "cz"},
        ],
    )

    assert added == ["dev-2"]
    assert updated == []          # dev-1 was byte-identical; nothing to rewrite
    assert len(entries) == 2
    assert entries[1] == {"id": "dev-2", "name": "New plug", "key": "k2", "category": "cz"}


def test_merge_reconciles_every_duplicate_row_for_one_device() -> None:
    """devices.json may hold a wizard row *and* a snapshot row for one device."""
    entries = [
        {"id": "dev-1", "key": "old-key"},
        {"id": "dev-1", "key": "old-key", "ip": "192.168.0.50"},
    ]

    _merge_cloud_rows(entries, [{"id": "dev-1", "key": "new-key"}])

    assert [row["key"] for row in entries] == ["new-key", "new-key"]
    assert entries[1]["ip"] == "192.168.0.50"


def test_merge_ignores_rows_with_no_id() -> None:
    entries = [_existing_row()]

    added, updated = _merge_cloud_rows(entries, [{"name": "orphan", "key": "k"}])

    assert (added, updated) == ([], [])
    assert len(entries) == 1


# ------------------------------------------------------------------- syncing
@pytest.fixture
def devices_file(tmp_path, monkeypatch):
    """Point ``tuya_client._DEVICE_FILE`` at a throwaway devices.json."""
    path = tmp_path / "devices.json"
    path.write_text(json.dumps([_existing_row()]), encoding="utf-8")
    monkeypatch.setattr(tuya_client, "_DEVICE_FILE", path)
    return path


def test_sync_writes_the_merged_list_and_keeps_the_stored_address(
    devices_file, monkeypatch
) -> None:
    monkeypatch.setattr(
        tuya_cloud,
        "_fetch_cloud_rows",
        lambda entries: [
            {"id": "dev-1", "key": "rotated"},
            {"id": "dev-2", "name": "New plug", "key": "k2"},
        ],
    )

    result = sync_devices_from_cloud()

    assert result == {"added": ["dev-2"], "updated": ["dev-1"], "total": 2, "changed": True}
    rows = json.loads(devices_file.read_text(encoding="utf-8"))
    assert [row["id"] for row in rows] == ["dev-1", "dev-2"]
    assert rows[0]["ip"] == "192.168.0.50"
    assert rows[0]["key"] == "rotated"


def test_sync_leaves_the_file_untouched_when_nothing_changed(
    devices_file, monkeypatch
) -> None:
    before = devices_file.read_bytes()
    monkeypatch.setattr(
        tuya_cloud, "_fetch_cloud_rows", lambda entries: [{"id": "dev-1", "key": "old-key"}]
    )

    result = sync_devices_from_cloud()

    assert result["changed"] is False
    assert devices_file.read_bytes() == before


def test_sync_preserves_the_snapshot_wrapper_shape(tmp_path, monkeypatch) -> None:
    """``{"timestamp": ..., "devices": [...]}`` must be written back as such."""
    path = tmp_path / "devices.json"
    path.write_text(
        json.dumps({"timestamp": 1, "devices": [_existing_row()]}), encoding="utf-8"
    )
    monkeypatch.setattr(tuya_client, "_DEVICE_FILE", path)
    monkeypatch.setattr(
        tuya_cloud, "_fetch_cloud_rows", lambda entries: [{"id": "dev-2", "key": "k2"}]
    )

    sync_devices_from_cloud()

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["timestamp"] == 1
    assert [row["id"] for row in payload["devices"]] == ["dev-1", "dev-2"]


def test_sync_without_devices_json_points_at_the_one_time_wizard(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(tuya_client, "_DEVICE_FILE", tmp_path / "missing.json")

    with pytest.raises(TuyaCloudError, match="tinytuya wizard"):
        sync_devices_from_cloud()


# --------------------------------------------------------------- credentials
def test_missing_credentials_name_the_env_keys(monkeypatch) -> None:
    monkeypatch.setattr(tuya_cloud, "load_dotenv", lambda **_kw: None)
    monkeypatch.delenv("TUYA_API_KEY", raising=False)
    monkeypatch.setenv("TUYA_API_SECRET", "secret")

    with pytest.raises(TuyaCloudError, match="TUYA_API_KEY"):
        tuya_cloud._credentials()


def test_credentials_default_the_region(monkeypatch) -> None:
    monkeypatch.setattr(tuya_cloud, "load_dotenv", lambda **_kw: None)
    monkeypatch.setenv("TUYA_API_KEY", "key")
    monkeypatch.setenv("TUYA_API_SECRET", "secret")
    monkeypatch.delenv("TUYA_REGION", raising=False)

    assert tuya_cloud._credentials() == ("key", "secret", "eu")


def test_expired_subscription_gets_its_own_message() -> None:
    """A permission/1010 error reads nothing like a bad key — say so."""
    message = tuya_cloud._cloud_error_message({"Payload": "no permissions (code 1010)"})

    assert "iot.tuya.com" in message
    assert "subscription expired" in message


def test_generic_cloud_error_points_at_the_credentials() -> None:
    message = tuya_cloud._cloud_error_message({"Payload": "sign invalid"})

    assert "TUYA_API_KEY" in message
    assert "iot.tuya.com" not in message


def test_anchor_device_id_requires_a_captured_device() -> None:
    with pytest.raises(TuyaCloudError, match="tinytuya wizard"):
        tuya_cloud._anchor_device_id([{"name": "no id here"}])

    assert tuya_cloud._anchor_device_id([{}, {"id": "dev-9"}]) == "dev-9"
