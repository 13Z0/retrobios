"""Round 5 theme: the stylesheet fixes of package 1, read from its source.

Each test fails on the stylesheet as it was before the fix: detection tables
whose last column sat past the page at 1440 px, a header search box, title
and "On this page" that moved 12 px between a guide and a console page,
stacked rows 136 px tall for four short cells, a line 4 px under a button,
a heading touching its first line, the browser's own choice list, and a
crumb separator free to end a line alone.
"""
from __future__ import annotations

import re
import unittest

from tests.test_site_r2_css import CSS_PATH, _blocks, _strip_comments, _theme_tokens

STACK = ".sl-markdown-content table[data-rb-stack] td"


def _block(css: str, selector: str) -> str:
    bodies = [b for s, b in _blocks(css) if s == selector]
    if not bodies:
        raise AssertionError(f"no block {selector}")
    return "".join(bodies)


def _declarations(body: str) -> dict[str, str]:
    return {k.strip(): v.strip() for k, v in re.findall(r"([\w-]+)\s*:\s*([^;]+);", body)}


class ThemeStylesheetRound5(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.css = _strip_comments(CSS_PATH.read_text(encoding="utf-8"))

    # 1.1
    def test_long_code_in_a_cell_breaks_to_fit_its_column(self):
        self.assertIn("overflow-wrap: anywhere",
                      _block(self.css, ".sl-markdown-content table td code:not(.rb-short)"))
        last = _block(self.css, ".sl-markdown-content table:not([data-rb-stack='kv']) "
                                "td:last-child:not(:first-child):has(code:not(.rb-short))")
        self.assertIn("min-width: 9rem", last)
        # A flag or a short name in a cell still never splits.
        self.assertIn("white-space: pre", _block(self.css, ".sl-markdown-content :not(pre) > code.rb-short"))

    # 1.2
    def test_one_grid_for_every_page(self):
        self.assertEqual(_theme_tokens(self.css, ":root").get("--rb-grid-width"), "54rem")
        self.assertIn("--sl-content-width: var(--rb-grid-width)",
                      _block(self.css, "body:has(.rb-sys-file, .rb-file-entry)"))
        # The header places its search box from the grid, not the page's column.
        self.assertIn("--sl-content-width: var(--rb-grid-width)",
                      _block(self.css, ":root[data-has-sidebar] header.header"))
        toc = _block(self.css, ":root[data-has-sidebar] .right-sidebar-container")
        pane = _block(self.css, ":root[data-has-sidebar] .main-pane")
        for body in (toc, pane):
            self.assertIn("var(--rb-grid-width)", body)
            self.assertNotIn("var(--sl-content-width)", body)
        # Every column starts on the grid's edge, the narrow one included.
        self.assertIn("--sl-content-margin-inline: max(0rem, 100% - var(--rb-grid-width)) auto", pane)
        # The pane rule covers pages with and without "On this page" alike.
        self.assertFalse([s for s, _ in _blocks(self.css) if s.endswith(":not([data-has-toc]) .main-pane")])

    # 1.3
    def test_stacked_rows_are_short(self):
        cell = _declarations(_block(self.css, STACK))
        self.assertEqual(cell["font-size"], "0.875rem")
        self.assertEqual(cell["line-height"], "1.35")
        label = _declarations(_block(self.css, STACK + "::before"))
        self.assertEqual(label["width"], "max-content")
        self.assertEqual(label["min-width"], "6.5rem")
        self.assertEqual(label["max-width"], "9rem")
        self.assertEqual(label["font-size"], "0.8125rem")
        # Values start in the column of the shortest label: the label's
        # negative margin is the cell's left padding.
        indent = cell["padding"].split()[3]
        self.assertEqual(label["margin"].split()[3], f"-{indent}")
        self.assertEqual(indent, "7.1rem")
        self.assertIn("padding: 0.3rem 0", _block(self.css, ".sl-markdown-content table[data-rb-stack] tr"))
        # A two-column list keeps body text.
        self.assertIn("font-size: 1rem", _block(self.css, ".sl-markdown-content table[data-rb-stack='kv'] td"))

    # 1.5
    def test_spacing_after_a_button_and_a_search_marker(self):
        self.assertIn("margin-top: 1rem",
                      _block(self.css, ".sl-markdown-content :is(.rb-button, p:has(> .rb-button)) + p"))
        self.assertIn("margin-top: var(--sl-content-gap-y)",
                      _block(self.css, ".sl-markdown-content span[data-pagefind-meta]:empty + :is(p, ul, ol, table, div)"))

    def test_choice_list_matches_the_filter_box(self):
        select = _declarations(_block(self.css, ".sl-markdown-content :is(.rb-file-lookup, .rb-picker) select,\n"
                                                ".sl-markdown-content select"))
        box = _declarations(_block(self.css, ".rb-table-filter input"))
        for prop in ("border", "border-radius", "min-height"):
            with self.subTest(prop=prop):
                self.assertEqual(select[prop], box[prop])
        self.assertEqual(select["appearance"], "none")
        self.assertIn("var(--rb-select-arrow)", select["background"])
        dark = _theme_tokens(self.css, ":root")["--rb-select-arrow"]
        light = _theme_tokens(self.css, ":root[data-theme='light']")["--rb-select-arrow"]
        # The arrow is drawn in each theme's secondary text colour.
        for arrow, tokens in ((dark, _theme_tokens(self.css, ":root")),
                              (light, _theme_tokens(self.css, ":root[data-theme='light']"))):
            colour = tokens["--rb-text-secondary"].lstrip("#")
            self.assertIn(f"%23{colour}", arrow)

    def test_crumb_separator_stays_with_its_label(self):
        self.assertIn("white-space: nowrap", _block(self.css, ".rb-breadcrumbs li"))
        self.assertIn("white-space: normal", _block(self.css, ".rb-breadcrumbs li > :is(a, span)"))


if __name__ == "__main__":
    unittest.main()
