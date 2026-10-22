#!/usr/bin/env python3
"""Write the site's own mark: header logo, favicon and touch icon.

The mark is the chip of the project logo drawn as vector shapes: a body,
four pins per side and the pixel glyph on a 4 by 4 grid. Three variants:

- ``logo.svg``: white, the glyph cut out so the header colour shows through.
  It sits on the indigo header in both schemes.
- ``favicon.svg``: indigo chip with a cyan glyph, lighter in a dark browser.
- ``favicon-32.png`` and ``apple-touch-icon.png`` (180 px, opaque): the same
  shapes rasterised by a headless Chromium through Playwright, for browsers
  that take no SVG icon and for home-screen shortcuts.

The files land in ``docs_assets/brand/``, which generate_site.py copies into
the site. The site build never runs this script.

    python scripts/make_site_icons.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs_assets" / "brand"

HEADER = "#3b3f7a"
GLYPH = "#8be0f7"
GLYPH_DIM = "#4fa3c4"

# (column, row, dim) on the 4 by 4 grid, read from the project logo.
PIXELS = [
    (0, 0, False), (1, 0, False), (2, 0, True),
    (1, 1, False), (3, 1, True),
    (0, 2, False), (3, 2, False),
    (1, 3, True), (2, 3, False), (3, 3, False),
]

BODY = (8, 8, 32, 4)  # x, y, size, corner radius in a 48 unit box
PIN_CENTRES = (13.5, 20.5, 27.5, 34.5)
PIN_WIDTH = 3.4
PIN_LENGTH = 5.5
CELL = 5.0
GAP = 1.6


def _pins(fill: str) -> str:
    half = PIN_WIDTH / 2
    rects = []
    for centre in PIN_CENTRES:
        start = centre - half
        rects += [
            (start, 8 - PIN_LENGTH, PIN_WIDTH, PIN_LENGTH),
            (start, 40, PIN_WIDTH, PIN_LENGTH),
            (8 - PIN_LENGTH, start, PIN_LENGTH, PIN_WIDTH),
            (40, start, PIN_LENGTH, PIN_WIDTH),
        ]
    return "".join(
        f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" rx="0.8" fill="{fill}"/>'
        for x, y, w, h in rects
    )


def _glyph(bright: str, dim: str) -> str:
    span = 4 * CELL + 3 * GAP
    origin = BODY[0] + (BODY[2] - span) / 2
    return "".join(
        f'<rect x="{origin + c * (CELL + GAP):g}" y="{origin + r * (CELL + GAP):g}" '
        f'width="{CELL:g}" height="{CELL:g}" rx="0.6" fill="{dim if is_dim else bright}"/>'
        for c, r, is_dim in PIXELS
    )


def _body(fill: str, extra: str = "") -> str:
    x, y, size, radius = BODY
    return f'<rect x="{x}" y="{y}" width="{size}" height="{size}" rx="{radius}" fill="{fill}"{extra}/>'


def logo_svg() -> str:
    """White chip, glyph cut out: the header colour shows through it."""
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" role="img" aria-label="RetroBIOS">'
        '<mask id="glyph">' + _body("#fff") + _glyph("#000", "#5a5a5a") + "</mask>"
        + _pins("#fff") + _body("#fff", ' mask="url(#glyph)"') + "</svg>"
    )


def favicon_svg() -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48">'
        "<style>.c{fill:" + HEADER + "}"
        "@media (prefers-color-scheme: dark){.c{fill:#8a8fd6}}</style>"
        + _pins(HEADER).replace(f'fill="{HEADER}"', 'class="c"')
        + _body(HEADER).replace(f'fill="{HEADER}"', 'class="c"')
        + _glyph(GLYPH, GLYPH_DIM) + "</svg>"
    )


def touch_icon_svg() -> str:
    """Opaque square: iOS fills transparency with black."""
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="-6 -6 60 60">'
        f'<rect x="-6" y="-6" width="60" height="60" fill="{HEADER}"/>'
        + logo_svg().split(">", 1)[1].rsplit("</svg>", 1)[0] + "</svg>"
    )


def main() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit("Playwright with Chromium is needed: pip install playwright") from exc
    OUT.mkdir(parents=True, exist_ok=True)
    for name, text in (("logo.svg", logo_svg()), ("favicon.svg", favicon_svg())):
        (OUT / name).write_text(text + "\n", encoding="utf-8")
        print((OUT / name).relative_to(ROOT))
    rasters = (
        ("favicon-32.png", 32, favicon_svg().replace("@media (prefers-color-scheme: dark)", "@media not all")),
        ("apple-touch-icon.png", 180, touch_icon_svg()),
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for name, size, text in rasters:
            page = browser.new_page(viewport={"width": size, "height": size})
            sized = text.replace("<svg ", f'<svg width="{size}" height="{size}" ', 1)
            page.set_content(f'<html><body style="margin:0;background:transparent">{sized}</body></html>')
            page.screenshot(path=str(OUT / name), omit_background=True, clip={"x": 0, "y": 0, "width": size, "height": size})
            page.close()
            print((OUT / name).relative_to(ROOT))
        browser.close()


if __name__ == "__main__":
    from common import run_cli

    run_cli(main)
