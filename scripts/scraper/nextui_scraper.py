#!/usr/bin/env python3
"""Scraper for NextUI's pak list: the MinUI scraper on NextUI's repository."""

from __future__ import annotations

try:
    from . import minui_scraper
except ImportError:
    import minui_scraper

PLATFORM_NAME = "nextui"


class Scraper(minui_scraper.Scraper):
    """Pak scraper for NextUI."""

    variant = minui_scraper.VARIANTS["nextui"]


def main() -> None:
    try:
        from .base_scraper import scraper_cli
    except ImportError:
        from base_scraper import scraper_cli

    scraper_cli(Scraper, "Scrape the NextUI pak list")


if __name__ == "__main__":
    main()
