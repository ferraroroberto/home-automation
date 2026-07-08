"""Unit tests for fleet-hash cache-busting, including subdirectory assets."""

from __future__ import annotations

from pathlib import Path

from src.static_versioning import (
    BuildInfo,
    compute_asset_hashes,
    rewrite_index_html,
    rewrite_js_imports,
)


def _make_static_tree(tmp_path: Path) -> Path:
    static_dir = tmp_path / "static"
    (static_dir / "_vendored" / "nav").mkdir(parents=True)
    (static_dir / "_vendored" / "icons").mkdir(parents=True)
    (static_dir / "_vendored" / "empty-state").mkdir(parents=True)
    (static_dir / "main.js").write_text(
        "import { icon } from './_vendored/icons/icons.js';\n", encoding="utf-8"
    )
    (static_dir / "_vendored" / "nav" / "nav-tabs.css").write_text(
        "nav{}", encoding="utf-8"
    )
    (static_dir / "_vendored" / "icons" / "icons.js").write_text(
        "export const icon = 1;", encoding="utf-8"
    )
    (static_dir / "_vendored" / "empty-state" / "empty-state.js").write_text(
        "import { icon } from '../icons/icons.js';\n", encoding="utf-8"
    )
    return static_dir


def test_rewrite_index_html_stamps_vendored_subdir_css(tmp_path: Path) -> None:
    static_dir = _make_static_tree(tmp_path)
    hashes = compute_asset_hashes(static_dir)
    body = '<link href="/static/_vendored/nav/nav-tabs.css" rel="stylesheet">'
    stamped = rewrite_index_html(body, hashes)
    assert "/static/_vendored/nav/nav-tabs.css?v=" in stamped


def test_rewrite_js_imports_stamps_subdir_import_from_root(tmp_path: Path) -> None:
    static_dir = _make_static_tree(tmp_path)
    hashes = compute_asset_hashes(static_dir)
    body = "import { icon } from './_vendored/icons/icons.js';\n"
    stamped = rewrite_js_imports(body, hashes, from_dir="")
    assert "./_vendored/icons/icons.js?v=" in stamped


def test_rewrite_js_imports_stamps_parent_relative_import(tmp_path: Path) -> None:
    static_dir = _make_static_tree(tmp_path)
    hashes = compute_asset_hashes(static_dir)
    body = "import { icon } from '../icons/icons.js';\n"
    stamped = rewrite_js_imports(
        body, hashes, from_dir="_vendored/empty-state"
    )
    assert "../icons/icons.js?v=" in stamped


def test_compute_asset_hashes_keyed_by_relpath_avoids_basename_collision(
    tmp_path: Path,
) -> None:
    static_dir = tmp_path / "static"
    (static_dir / "a").mkdir(parents=True)
    (static_dir / "b").mkdir(parents=True)
    (static_dir / "a" / "icons.js").write_text("a", encoding="utf-8")
    (static_dir / "b" / "icons.js").write_text("b", encoding="utf-8")
    hashes = compute_asset_hashes(static_dir)
    assert set(hashes) == {"a/icons.js", "b/icons.js"}


def test_build_info_stamp_js_resolves_against_source_path(
    tmp_path: Path,
) -> None:
    static_dir = _make_static_tree(tmp_path)
    build_info = BuildInfo(static_dir, tmp_path)
    body = "import { icon } from '../icons/icons.js';\n"
    source_path = static_dir / "_vendored" / "empty-state" / "empty-state.js"
    stamped = build_info.stamp_js(body, source_path)
    assert "../icons/icons.js?v=" in stamped


def test_vendor_dir_still_unstamped(tmp_path: Path) -> None:
    static_dir = tmp_path / "static"
    (static_dir / "vendor").mkdir(parents=True)
    (static_dir / "vendor" / "chart.js").write_text("chart", encoding="utf-8")
    hashes = compute_asset_hashes(static_dir)
    body = '<script src="/static/vendor/chart.js"></script>'
    assert rewrite_index_html(body, hashes) == body
