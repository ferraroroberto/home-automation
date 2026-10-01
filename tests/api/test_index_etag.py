"""Entry-document revalidation (#757): ``/`` carries an ETag and answers a
matching ``If-None-Match`` with a body-less 304.

The ETag is taken over the stamped body and the cache is keyed on the file's
mtime and size, so an edit on disk changes it without a restart.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def test_index_etag_round_trip_is_a_bodyless_304(client: TestClient) -> None:
    first = client.get("/")
    assert first.status_code == 200
    etag = first.headers["etag"]
    assert first.headers["cache-control"] == "no-cache, must-revalidate"

    again = client.get("/", headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.content == b""
    assert again.headers["etag"] == etag
    assert again.headers["cache-control"] == "no-cache, must-revalidate"


@pytest.mark.parametrize("form", ["W/{etag}", '"stale", {etag}', "*"])
def test_index_etag_tolerates_weak_lists_and_star(client: TestClient, form: str) -> None:
    etag = client.get("/").headers["etag"]
    resp = client.get("/", headers={"If-None-Match": form.format(etag=etag)})
    assert resp.status_code == 304


def test_index_stale_etag_gets_the_full_body(client: TestClient) -> None:
    resp = client.get("/", headers={"If-None-Match": '"not-the-current-one"'})
    assert resp.status_code == 200
    assert "<html" in resp.text.lower()


def test_index_edit_on_disk_changes_the_etag(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.webapp.routers import misc

    index = tmp_path / "index.html"
    index.write_text("<html><body>one</body></html>", encoding="utf-8")
    monkeypatch.setattr(misc, "STATIC_DIR", tmp_path)

    before = client.get("/")
    assert "one" in before.text

    index.write_text("<html><body>two, edited</body></html>", encoding="utf-8")
    st = index.stat()
    os.utime(index, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

    after = client.get("/", headers={"If-None-Match": before.headers["etag"]})
    assert after.status_code == 200
    assert "two, edited" in after.text
    assert after.headers["etag"] != before.headers["etag"]
