"""Generate PWA/tray/Stream-Deck icons from the shared fleet icon-brand generator.

Thin caller onto ``project-scaffolding``'s ``brand_gen.render_set()`` — the
master art is home-automation's vendored Lucide ``house.svg``, not a
bespoke Pillow-drawn silhouette (a coherent icon family across the fleet).
The vendored master is rendered via resvg-py rather than hand-derived
proportions in Pillow, so every project's icon is provably the same
vocabulary as the in-app Lucide nav icons, in the fleet's monochrome look.

Writes into ``app/webapp/static/``: ``icon-512.png``, ``icon-512-maskable.png``,
``icon-180.png``, ``icon-192.png``, ``favicon.ico``. Into ``assets/tray/``:
``home-automation.ico``. Into ``assets/stream-deck/``: ``home-automation-144.png``.

Usage:
    & .\\.venv\\Scripts\\python.exe scripts\\gen_icons.py

The ``project-scaffolding`` checkout is located via the ``PROJECT_SCAFFOLDING_ROOT``
environment variable, defaulting to the ``project-scaffolding`` folder beside this checkout.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Overridable so this script (and its master art) work outside this single
# developer's machine layout — falls back to the sibling checkout when
# the env var isn't set. Same `PROJECT_SCAFFOLDING_ROOT` spelling
# as the sister repos' own `gen_icons.py`, so one export covers the fleet.
SCAFFOLDING_ROOT = Path(
    os.environ.get("PROJECT_SCAFFOLDING_ROOT")
    or Path(__file__).resolve().parent.parent.parent / "project-scaffolding"
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
