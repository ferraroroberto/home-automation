"""Response helpers shared by the API-stub fixtures."""

from __future__ import annotations

import json
from typing import Optional
from urllib.parse import unquote


def json_body(obj) -> str:
    return json.dumps(obj)


def device_mac(url: str) -> str:
    """The MAC path segment of a /api/network/devices/<mac>/<field> URL."""
    return unquote(url.split("/api/network/devices/", 1)[1].split("/", 1)[0])


def text_or_none(value) -> Optional[str]:
    """A PUT'd free-text field: trimmed, with blank normalised to None."""
    return (value or "").strip() or None


def patch_field(route, items, id_field, id_value, field, value) -> None:
    """Set one field on the item `id_value` identifies, and echo the write back.

    Every single-field PUT the network mock serves (device display_name/hidden/
    group, wifi-bssid display_name/hidden) is this same shape, so they all route
    through here rather than hand-rolling find-set-fulfil five times over.
    """
    for item in items:
        if item[id_field] == id_value:
            item[field] = value
    route.fulfill(
        status=200,
        content_type="application/json",
        body=json_body({id_field: id_value, field: value}),
    )
