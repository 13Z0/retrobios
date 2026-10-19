"""MkDocs hooks for the generated site.

The sitemap gave all 664 pages the build date as lastmod, so the field told
a crawler nothing. A page whose content carries its own date (an emulator
page: the day its profile was last read against the source) states it in
its front matter. The rest get no lastmod rather than an inexact one.
"""

from __future__ import annotations

from typing import Any


def on_page_markdown(markdown: str, page: Any, config: Any, files: Any) -> str:  # noqa: ARG001
    """Take lastmod from the page's own date, or leave it out."""
    page.update_date = str(page.meta.get("date_modified") or "")
    return markdown
