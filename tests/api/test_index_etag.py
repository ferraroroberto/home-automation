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


def test_index_includes_view_partials_in_place(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.webapp.routers import misc

    (tmp_path / "views").mkdir()
    (tmp_path / "views" / "pane.html").write_text("<p>pane</p>\n", encoding="utf-8")
    (tmp_path / "index.html").write_text(
        "<body>\n  <!-- @include views/pane.html -->\n</body>\n", encoding="utf-8"
    )
    monkeypatch.setattr(misc, "STATIC_DIR", tmp_path)

    assert client.get("/").text == "<body>\n<p>pane</p>\n</body>\n"


def test_index_edit_of_a_partial_changes_the_etag(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.webapp.routers import misc

    (tmp_path / "views").mkdir()
    partial = tmp_path / "views" / "pane.html"
    partial.write_text("<p>one</p>\n", encoding="utf-8")
    (tmp_path / "index.html").write_text(
        "<!-- @include views/pane.html -->\n", encoding="utf-8"
    )
    monkeypatch.setattr(misc, "STATIC_DIR", tmp_path)

    before = client.get("/")
    partial.write_text("<p>two, edited</p>\n", encoding="utf-8")
    st = partial.stat()
    os.utime(partial, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

    after = client.get("/", headers={"If-None-Match": before.headers["etag"]})
    assert after.status_code == 200
    assert "two, edited" in after.text
    assert after.headers["etag"] != before.headers["etag"]


@pytest.mark.parametrize("target", ["views/missing.html", "../outside.html"])
def test_index_include_that_cannot_be_read_fails_loudly(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: str
) -> None:
    from app.webapp.routers import misc

    static = tmp_path / "static"
    static.mkdir()
    (tmp_path / "outside.html").write_text("<p>secret</p>\n", encoding="utf-8")
    (static / "index.html").write_text(
        f"<!-- @include {target} -->\n", encoding="utf-8"
    )
    monkeypatch.setattr(misc, "STATIC_DIR", static)

    resp = client.get("/")
    assert resp.status_code == 500
    assert "secret" not in resp.text


def test_real_index_has_no_unexpanded_include(client: TestClient) -> None:
    assert "@include" not in client.get("/").text
