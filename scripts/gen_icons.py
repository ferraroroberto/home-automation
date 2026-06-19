"""Generate the PWA icons (a simple house glyph on a dark tile).

Writes into ``app/webapp/static/``:

    icon-512.png            512x512, full-bleed tile (purpose: any)
    icon-512-maskable.png   512x512, safe-zone padded (purpose: maskable)
    icon-180.png            iOS apple-touch-icon
    favicon.ico             multi-size favicon

Pillow is a dev-only dependency for this one-shot — the generated PNGs
are committed, so the webapp never imports Pillow at runtime.

Usage:
    python scripts/gen_icons.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "webapp" / "static"

BG = (10, 15, 26)        # --bg
ACCENT = (47, 125, 246)  # --accent
WHITE = (244, 247, 251)


def _house(size: int, pad_ratio: float) -> Image.Image:
    """Draw a house glyph on a full-bleed, opaque dark tile.

    Full-bleed + opaque (RGB, no alpha, no transparent corners) is required
    for the iOS apple-touch-icon: iOS composites any alpha against black and
    applies its own squircle corner mask, so a transparent-cornered RGBA icon
    renders as an invisible black square on a dark home screen. Android/Chrome
    apply the maskable safe-zone + corner mask themselves, so a flat opaque
    square is the correct source for every target.

    The glyph is a white house silhouette (gabled roof + square body) with an
    accent-coloured door, centred in a padded safe zone.
    """
    img = Image.new("RGB", (size, size), BG)
    d = ImageDraw.Draw(img)

    # Glyph geometry inside a padded safe zone.
    pad = int(size * pad_ratio)
    left = pad
    right = size - pad
    top = pad
    bottom = size - pad
    cx = size // 2
    span = right - left

    # Roof: a gable triangle spanning the full width, apex at the top.
    eaves_y = top + int(span * 0.42)  # where the roof meets the walls
    d.polygon(
        [(left, eaves_y), (cx, top), (right, eaves_y)],
        fill=WHITE,
    )

    # Body: a square wall block under the eaves, inset so the roof overhangs.
    body_inset = int(span * 0.12)
    body_left = left + body_inset
    body_right = right - body_inset
    d.rectangle([body_left, eaves_y, body_right, bottom], fill=WHITE)

    # Door: a tall accent-coloured opening, bottom-centred in the body.
    door_w = int(span * 0.22)
    door_h = int((bottom - eaves_y) * 0.62)
    door_top = bottom - door_h
    d.rounded_rectangle(
        [cx - door_w // 2, door_top, cx + door_w // 2, bottom],
        radius=max(2, door_w // 6),
        fill=ACCENT,
    )
    return img


def main() -> int:
    STATIC_DIR.mkdir(parents=True, exist_ok=True)

    icon = _house(512, pad_ratio=0.18)
    icon.save(STATIC_DIR / "icon-512.png")

    maskable = _house(512, pad_ratio=0.28)  # extra safe-zone padding
    maskable.save(STATIC_DIR / "icon-512-maskable.png")

    icon.resize((180, 180), Image.LANCZOS).save(STATIC_DIR / "icon-180.png")

    icon.resize((64, 64), Image.LANCZOS).save(
        STATIC_DIR / "favicon.ico",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64)],
    )

    print(f"✅ wrote icons into {STATIC_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
