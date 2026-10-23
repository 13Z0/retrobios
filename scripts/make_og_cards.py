#!/usr/bin/env python3
"""Render the sharing cards (Open Graph images) to PNG, once.

Each card is an SVG written by ``sitediagrams.og_card_svg`` from the
registry, rasterised by a headless Chromium through Playwright, then
committed under ``docs_assets/og/``. The site build copies the PNG files and
never runs this script, so it adds nothing to the build's dependencies.
Run it again when a section's wording, the platform count or the list of
platforms changes.

    python scripts/make_og_cards.py
    python scripts/make_og_cards.py --only platforms

A platform page shares a card with its own name (``og/platforms/<id>.png``),
the other pages their section's.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from common import list_registered_platforms, load_platform_config  # noqa: E402
from sitediagrams import OG_SECTIONS, og_card_svg  # noqa: E402

OUT = ROOT / "docs_assets" / "og"
LOGO = ROOT / ".github" / "assets" / "logo.png"
PLATFORM_TAGLINE = "Which BIOS files it needs, and where each one goes"


def platform_cards() -> dict[str, str]:
    """Each platform page's card, by its path under og/, with its title."""
    platforms = ROOT / "platforms"
    return {
        f"platforms/{name}": load_platform_config(name, str(platforms))["platform"]
        for name in list_registered_platforms(str(platforms), include_archived=True)
    }


def card_svg(key: str, logo: bytes, titles: dict[str, str]) -> str:
    if key in OG_SECTIONS:
        return og_card_svg(key, logo)
    # A platform card is a section card with the platform's name for title.
    OG_SECTIONS[key] = (titles[key], PLATFORM_TAGLINE)
    try:
        return og_card_svg(key, logo)
    finally:
        del OG_SECTIONS[key]


def main() -> None:
    parser = argparse.ArgumentParser(description="Render the sharing cards to PNG.",
                                     allow_abbrev=False)
    parser.add_argument("--only", choices=("sections", "platforms"),
                        help="render one kind of card (default: both)")
    args = parser.parse_args()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit("Playwright with Chromium is needed: pip install playwright") from exc
    titles = platform_cards()
    keys = [*([] if args.only == "platforms" else OG_SECTIONS),
            *([] if args.only == "sections" else titles)]
    logo = LOGO.read_bytes()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1200, "height": 630})
        for key in keys:
            page.set_content(
                '<html><body style="margin:0">' + card_svg(key, logo, titles) + "</body></html>"
            )
            path = OUT / f"{key}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(path), clip={"x": 0, "y": 0, "width": 1200, "height": 630})
            print(path.relative_to(ROOT))
        browser.close()


if __name__ == "__main__":
    from common import run_cli

    run_cli(main)
