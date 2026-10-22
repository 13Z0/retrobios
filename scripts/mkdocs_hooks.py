"""MkDocs hooks for the generated site.

The sitemap gave all 664 pages the build date as lastmod, so the field told
a crawler nothing. A page whose content carries its own date (an emulator
page: the day its profile was last read against the source) states it in
its front matter. The rest get no lastmod rather than an inexact one.
"""

from __future__ import annotations

from typing import Any

# Pages made of file cards, level 3 headings by the hundred: their table of
# contents stops at level 2, the consoles of a system page, the Files of an
# emulator, the sections of a platform page (which listed every system id).
CARD_PAGES = ("systems/", "emulators/", "platforms/")


def on_page_markdown(markdown: str, page: Any, config: Any, files: Any) -> str:  # noqa: ARG001
    """Take lastmod from the page's own date, or leave it out."""
    page.update_date = str(page.meta.get("date_modified") or "")
    return markdown


def _prune(items: list, depth: int) -> None:
    for item in items:
        if item.level >= depth:
            item.children = []
        else:
            _prune(item.children, depth)


def on_page_content(html: str, page: Any, config: Any, files: Any) -> str:  # noqa: ARG001
    """Keep the table of contents of a page of cards to level 2."""
    if page.file.src_uri.startswith(CARD_PAGES):
        _prune(page.toc.items, 2)
    return html
