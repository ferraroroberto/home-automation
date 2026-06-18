"""Generate the PWA icons (a simple thermometer glyph on a dark tile).

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


def _thermometer(size: int, pad_ratio: float) -> Image.Image:
    """Draw a thermometer glyph centred on a rounded dark tile."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Rounded background tile.
    radius = int(size * 0.22)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=BG)

    # Glyph geometry inside a padded safe zone.
    pad = int(size * pad_ratio)
    cx = size // 2
    stem_w = int(size * 0.12)
    bulb_r = int(size * 0.13)
    top_y = pad + bulb_r // 2
    bulb_cy = size - pad - bulb_r

    # Stem (rounded) in white.
    d.rounded_rectangle(
        [cx - stem_w // 2, top_y, cx + stem_w // 2, bulb_cy],
        radius=stem_w // 2,
        fill=WHITE,
    )
    # Bulb outline in white.
    d.ellipse(
        [cx - bulb_r, bulb_cy - bulb_r, cx + bulb_r, bulb_cy + bulb_r],
        fill=WHITE,
    )
    # Coloured mercury: inner fill + bulb.
    inner_w = max(2, int(stem_w * 0.42))
    inner_top = int(size * 0.42)
    d.rounded_rectangle(
        [cx - inner_w // 2, inner_top, cx + inner_w // 2, bulb_cy],
        radius=inner_w // 2,
        fill=ACCENT,
    )
    inner_r = int(bulb_r * 0.62)
    d.ellipse(
        [cx - inner_r, bulb_cy - inner_r, cx + inner_r, bulb_cy + inner_r],
        fill=ACCENT,
    )
    return img


def main() -> int:
    STATIC_DIR.mkdir(parents=True, exist_ok=True)

    icon = _thermometer(512, pad_ratio=0.18)
    icon.save(STATIC_DIR / "icon-512.png")

    maskable = _thermometer(512, pad_ratio=0.28)  # extra safe-zone padding
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
