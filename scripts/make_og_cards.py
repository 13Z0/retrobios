#!/usr/bin/env python3
"""Render the sharing cards (Open Graph images) to PNG, once.

Each card is an SVG written by ``sitediagrams.og_card_svg`` from the
registry, rasterised by a headless Chromium through Playwright, then
committed under ``docs_assets/og/``. The site build copies the PNG files and
never runs this script, so it adds nothing to the build's dependencies.
Run it again when a section's wording or the platform count changes.

    python scripts/make_og_cards.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from sitediagrams import OG_SECTIONS, og_card_svg  # noqa: E402

OUT = ROOT / "docs_assets" / "og"
LOGO = ROOT / ".github" / "assets" / "logo.png"


def main() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit("Playwright with Chromium is needed: pip install playwright") from exc
    OUT.mkdir(parents=True, exist_ok=True)
    logo = LOGO.read_bytes()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1200, "height": 630})
        for section in OG_SECTIONS:
            page.set_content(
                '<html><body style="margin:0">' + og_card_svg(section, logo) + "</body></html>"
            )
            path = OUT / f"{section}.png"
            page.screenshot(path=str(path), clip={"x": 0, "y": 0, "width": 1200, "height": 630})
            print(path.relative_to(ROOT))
        browser.close()


if __name__ == "__main__":
    from common import run_cli

    run_cli(main)
