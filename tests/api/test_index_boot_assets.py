"""The entry document links only the stylesheets a cold launch needs (#777).

Leaflet's script has loaded on first use since #760; its stylesheet is a
render-blocking request the Home view never uses, so it must load with the
script, not from ``<head>``.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

_STYLESHEET_HREFS = re.compile(r"""<link[^>]+rel=["']stylesheet["'][^>]*href=["']([^"']+)""")


def test_index_does_not_link_the_map_stylesheet(client: TestClient) -> None:
    hrefs = _STYLESHEET_HREFS.findall(client.get("/").text)
    assert any(h.startswith("/static/styles.css") for h in hrefs)  # the regex sees the real links
    assert [h for h in hrefs if "leaflet" in h] == []
