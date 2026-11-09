"""Unpainted row-verb assertion, local to this repo.

Lives beside the vendored ``_geometry.py`` rather than inside it so that file stays
byte-identical to project-scaffolding. Drop this module once the scaffold ships the
same assertion.
"""

from __future__ import annotations

from playwright.sync_api import Locator, Page

_UNPAINTED_JS = """els => els.map(el => {
  const cs = getComputedStyle(el);
  const borders = ['Top', 'Right', 'Bottom', 'Left']
    .reduce((sum, side) => sum + parseFloat(cs['border' + side + 'Width']), 0);
  return {
    fill: cs.backgroundColor,
    image: cs.backgroundImage,
    borders,
    shadow: cs.boxShadow,
  };
})"""


def assert_unpainted(page: Page, locator: Locator) -> None:
    """Every match is a plain icon-button at rest: no fill, border or shadow,
    in both themes (the verb is a glyph on nothing).

    The dark leg stamps ``dataset.theme`` for a computed-style read only — the
    palette is pure CSS tokens, so no boot mechanism is under test here.
    """
    original = page.evaluate("document.documentElement.dataset.theme || ''")
    try:
        for theme in ("light", "dark"):
            page.evaluate("t => { document.documentElement.dataset.theme = t; }", theme)
            styles = locator.evaluate_all(_UNPAINTED_JS)
            assert styles, f"no matches for {locator}"
            for index, style in enumerate(styles):
                assert style["fill"] in ("rgba(0, 0, 0, 0)", "transparent"), (theme, index, style)
                assert style["image"] == "none", (theme, index, style)
                assert style["borders"] == 0, (theme, index, style)
                assert style["shadow"] == "none", (theme, index, style)
    finally:
        page.evaluate(
            "t => { if (t) document.documentElement.dataset.theme = t;"
            " else delete document.documentElement.dataset.theme; }",
            original,
        )
