"""Generate PWA/tray/Stream-Deck icons from the shared fleet icon-brand generator.

Thin caller onto ``project-scaffolding``'s ``brand_gen.render_set()`` — the
master art is home-automation's vendored Lucide ``house.svg``, not a
bespoke Pillow-drawn silhouette (app-launcher#65: a coherent icon family
across the fleet). Supersedes issue #309's "no SVG-rasterization dependency"
constraint: the fleet-wide decision that landed in app-launcher#65 is to
render the vendored master via resvg-py rather than hand-derive proportions
in Pillow, so every project's icon is provably the same vocabulary as the
in-app Lucide nav icons. Drops the previous accent-coloured door for the
fleet's monochrome look.

Writes into ``app/webapp/static/``: ``icon-512.png``, ``icon-512-maskable.png``,
``icon-180.png``, ``icon-192.png``, ``favicon.ico``. Into ``assets/tray/``:
``home-automation.ico``. Into ``assets/stream-deck/``: ``home-automation-144.png``.

Usage:
    python scripts/gen_icons.py

The ``project-scaffolding`` checkout is located via the ``PROJECT_SCAFFOLDING_ROOT``
environment variable, defaulting to ``E:\\automation\\project-scaffolding``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Overridable so this script (and its master art) work outside this single
# developer's machine layout — falls back to the historical absolute path when
# the env var isn't set (issue #633). Same `PROJECT_SCAFFOLDING_ROOT` spelling
# as the sister repos' own `gen_icons.py`, so one export covers the fleet.
SCAFFOLDING_ROOT = Path(
    os.environ.get("PROJECT_SCAFFOLDING_ROOT", r"E:\automation\project-scaffolding")
)
sys.path.insert(0, str(SCAFFOLDING_ROOT / "scripts"))

from brand_gen import render_set  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "app" / "webapp" / "static"


def main() -> None:
    render_set(
        master=SCAFFOLDING_ROOT / "brand" / "house.svg",
        out_dir=STATIC_DIR,
        tray_out_dir=PROJECT_ROOT / "assets" / "tray",
        stream_deck_out_dir=PROJECT_ROOT / "assets" / "stream-deck",
        project_slug="home-automation",
    )
    print(f"wrote icons to {STATIC_DIR}")


if __name__ == "__main__":
    main()
