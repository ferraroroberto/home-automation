"""
Tuya Cloud bootstrap
====================
Non-UI core for the *one* thing the LAN-only ``src.tuya_client`` cannot do:
capture a newly-paired device's identity and local key (issue #612).

Kept in its own module deliberately — ``tuya_client.py`` documents itself as
LAN-only ("no Tuya Cloud at runtime") and that contract stays true: nothing
here runs on a poll tick or a control action, only behind the explicit
"Add device" action in the PWA.

What this replaces is the ``python -m tinytuya wizard`` terminal round trip.
The wizard is an interactive ``print``/``input`` wrapper over two TinyTuya
Cloud calls, so it is driven directly here instead of shelled out to: no
subprocess, no stdin prompts, no QR scan. The QR scan people associate with
the wizard is the *one-time* app-account link at https://iot.tuya.com — once
that link exists, every device later paired in Smart Life shows up in
:meth:`tinytuya.Cloud.getdevices` automatically.

Credentials come from ``.env`` (``TUYA_API_KEY`` / ``TUYA_API_SECRET`` /
``TUYA_REGION``), the same ``load_dotenv``-then-``os.getenv`` shape as every
other client in ``src/``. They are passed to :class:`tinytuya.Cloud`
explicitly rather than letting it fall back to its own ``tinytuya.json``
config file, which does not exist in this project.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import tinytuya
from dotenv import load_dotenv

from src import tuya_client
from src._atomic_json import write_json_atomic

logger = logging.getLogger("tuya.cloud")

_DEFAULT_REGION = "eu"

# Fields the Tuya Cloud is authoritative for. Everything else already in a
# devices.json row is preserved untouched by _merge_cloud_rows — see its
# docstring for why that matters (LAN state would otherwise be destroyed).
_CLOUD_OWNED_FIELDS = (
    "key",
    "mapping",
    "name",
    "category",
    "product_id",
    "product_name",
    "model",
    "mac",
    "uuid",
    "sn",
    "sub",
    "biz_type",
    "icon",
)

# Never overwritten from a cloud row, even if one somehow carries them: these
# describe the device on *this* LAN, which the cloud has no view of.
_LOCAL_ONLY_FIELDS = ("ip", "address", "version", "dps")


class TuyaCloudError(RuntimeError):
    """Raised when the Tuya Cloud bootstrap cannot complete.

    Distinct from ``tuya_client``'s LAN errors: this always means the cloud
    round trip failed (missing credentials, rejected key/secret, expired IoT
    Core subscription), never that a device is offline.
    """


def _credentials() -> tuple[str, str, str]:
    """Read Tuya Cloud API credentials from ``.env``.

    Unlike the runtime clients, missing credentials *are* an error here: this
    only ever runs behind an explicit user action, so silently reporting
    "nothing found" would be indistinguishable from a genuinely empty account.
    """
    load_dotenv(override=True)
    api_key = (os.getenv("TUYA_API_KEY") or "").strip()
    api_secret = (os.getenv("TUYA_API_SECRET") or "").strip()
    region = (os.getenv("TUYA_REGION") or "").strip() or _DEFAULT_REGION
    missing = [
        name
        for name, value in (("TUYA_API_KEY", api_key), ("TUYA_API_SECRET", api_secret))
        if not value
    ]
    if missing:
        raise TuyaCloudError(
            f"Missing {' and '.join(missing)} in .env — add the Tuya IoT project's "
            "Access ID and Access Secret before syncing from the cloud."
        )
    return api_key, api_secret, region


def _anchor_device_id(entries: list[dict[str, Any]]) -> str:
    """Return a known device id, used to resolve the Tuya account's user id.

    TinyTuya asks the cloud "who owns this device?" to find the account whose
    device list to fetch, so at least one already-captured device is needed.
    The very first bootstrap (no ``devices.json`` at all) therefore still
    belongs to ``python -m tinytuya wizard`` — deliberately out of scope.
    """
    for device in entries:
        device_id = str(device.get("id") or device.get("dev_id") or "").strip()
        if device_id:
            return device_id
    raise TuyaCloudError(
        "devices.json has no device id to identify the Tuya account with. Run "
        "`python -m tinytuya wizard` once for the very first device."
    )


def _cloud_error_message(error: Any) -> str:
    """Turn TinyTuya's error dict into actionable text.

    An expired IoT Core subscription is by far the most common failure (the
    trial expires and every call starts returning a permission error), and it
    reads nothing like a credential problem — so it gets its own message.
    """
    payload = ""
    if isinstance(error, dict):
        payload = str(error.get("Payload") or error.get("Error") or "")
    payload = payload or str(error or "Unknown error")
    if "permission" in payload.lower() or "1010" in payload:
        return (
            f"Tuya Cloud rejected the request ({payload}). This usually means the "
            "IoT Core subscription expired — renew it at https://iot.tuya.com."
        )
    return f"Tuya Cloud rejected the request ({payload}). Check TUYA_API_KEY/TUYA_API_SECRET/TUYA_REGION."


def _read_payload() -> tuple[Any, list[dict[str, Any]]]:
    """Return ``devices.json``'s raw payload plus its device-row list.

    Both on-disk shapes are supported (a plain list, or TinyTuya's snapshot
    ``{"timestamp": ..., "devices": [...]}``) and the wrapper is preserved so
    a merge writes back the same shape it read.
    """
    # Resolved through the module, not a from-import: ``tuya_client`` owns the
    # path (tests monkeypatch it there, and a value-bound copy would silently
    # diverge from it).
    device_file = tuya_client._DEVICE_FILE
    if not device_file.exists():
        raise TuyaCloudError(
            "Missing devices.json — run `python -m tinytuya wizard` once to capture "
            "the first device before syncing from the cloud."
        )
    with device_file.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    entries = payload.get("devices", []) if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise TuyaCloudError("devices.json has no device list to merge into")
    return payload, [device for device in entries if isinstance(device, dict)]


def _merge_cloud_rows(
    entries: list[dict[str, Any]], cloud_rows: list[dict[str, Any]]
) -> tuple[list[str], list[str]]:
    """Merge cloud rows into ``entries`` in place; return ``(added, updated)`` ids.

    Deliberately does **not** just persist ``getdevices()``'s return value,
    which is lossy: rows TinyTuya considers "changed" are rebuilt by its
    ``filter_devices()`` from a field whitelist that has no ``ip`` and no
    ``dps``. Measured on this repo's own file, that silently dropped the
    stored LAN address of 4 of 7 devices that had one — after which those
    plugs read as offline until the next broadcast scan, and any captured
    ``dps`` snapshot (what ``tuya_client._fallback_energy_mappings`` reads
    when a device has no ``mapping``) would be gone for good.

    So the cloud is treated as authoritative for identity and capability
    only; every other field on an existing row survives untouched.
    """
    by_id: dict[str, list[dict[str, Any]]] = {}
    for device in entries:
        device_id = str(device.get("id") or device.get("dev_id") or "")
        if device_id:
            by_id.setdefault(device_id, []).append(device)

    added: list[str] = []
    updated: list[str] = []
    for row in cloud_rows:
        device_id = str(row.get("id") or "").strip()
        if not device_id:
            continue
        # Whitelist, not blacklist: a field the cloud has no business owning
        # (see _LOCAL_ONLY_FIELDS) can only be written by being added to
        # _CLOUD_OWNED_FIELDS, which the disjointness test forbids.
        fields = {name: row[name] for name in _CLOUD_OWNED_FIELDS if name in row}
        matches = by_id.get(device_id)
        if not matches:
            new_row = {"id": device_id, **fields}
            entries.append(new_row)
            by_id[device_id] = [new_row]
            added.append(device_id)
            continue
        # A device may legitimately have several rows (snapshot + wizard);
        # reconcile every one of them so they can't drift apart.
        changed = False
        for device in matches:
            for name, value in fields.items():
                if device.get(name) != value:
                    device[name] = value
                    changed = True
        if changed:
            updated.append(device_id)
    return added, updated


def _fetch_cloud_rows(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fetch the account's device list (with local keys + DPS mappings).

    ``oldlist`` is passed so TinyTuya only re-downloads the DP-name mapping
    for devices that actually changed, rather than one cloud call per device
    on every sync.
    """
    api_key, api_secret, region = _credentials()
    anchor = _anchor_device_id(entries)

    cloud = tinytuya.Cloud(
        apiRegion=region, apiKey=api_key, apiSecret=api_secret, apiDeviceID=anchor
    )
    if cloud.error:
        raise TuyaCloudError(_cloud_error_message(cloud.error))

    rows = cloud.getdevices(False, oldlist=entries, include_map=True)
    if not isinstance(rows, list):
        raise TuyaCloudError(_cloud_error_message(rows))
    return [row for row in rows if isinstance(row, dict)]


def sync_devices_from_cloud() -> dict[str, Any]:
    """Pull newly-paired devices from the Tuya Cloud into ``devices.json``.

    The in-app replacement for a ``python -m tinytuya wizard`` run: pair the
    plug in the Smart Life app, then trigger this and it appears on the Plugs
    card. Only identity and capability are written — the caller runs the
    existing LAN rescan afterwards to fill in the new device's address.

    Blocking (HTTP round trips to the Tuya Cloud); call it from a worker
    thread. Never logs or returns a local key.
    """
    payload, entries = _read_payload()
    cloud_rows = _fetch_cloud_rows(entries)
    added, updated = _merge_cloud_rows(entries, cloud_rows)

    if added or updated:
        if isinstance(payload, dict):
            payload["devices"] = entries
        else:
            payload = entries
        write_json_atomic(tuya_client._DEVICE_FILE, payload)
        logger.info(
            "💾 Tuya cloud sync: %d device(s) added, %d updated (%d total)",
            len(added), len(updated), len(entries),
        )
    else:
        logger.info("ℹ️ Tuya cloud sync: nothing new (%d device(s) already captured)", len(entries))

    return {
        "added": added,
        "updated": updated,
        "total": len(entries),
        "changed": bool(added or updated),
    }


__all__ = ["TuyaCloudError", "sync_devices_from_cloud"]
