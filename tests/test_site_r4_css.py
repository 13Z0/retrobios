"""Round 4 theme: the stylesheet fixes of package 1, read from its source.

Each test fails on the stylesheet as it was before the fix: OS tabs on one
hidden scrolling row, stacked table cells 16 px apart with labels drawn over
the next row, short flags split across lines, lone card links 19 px high, a
card label column too narrow for its longest label.
"""
from __future__ import annotations

import unittest

from tests.test_site_r2_css import CSS_PATH, _blocks, _rules, _strip_comments

STACK = ".sl-markdown-content table[data-rb-stack] td"


def _block(css: str, selector: str) -> str:
    bodies = [b for s, b in _blocks(css) if s == selector]
    if not bodies:
        raise AssertionError(f"no block {selector}")
    return "".join(bodies)


class ThemeStylesheetRound4(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = CSS_PATH.read_text(encoding="utf-8")
        cls.css = _strip_comments(cls.raw)

    # 1.1
    def test_os_tabs_wrap_and_never_hide(self):
        tablist = _block(self.css, "starlight-tabs [role='tablist']")
        for declaration in ("flex-wrap: wrap", "overflow-x: visible", "row-gap: 0.25rem"):
            with self.subTest(declaration=declaration):
                self.assertIn(declaration, tablist)
        self.assertNotIn("scrollbar-width: none", self.css)
        self.assertNotIn("::-webkit-scrollbar", self.css)

    # 1.2
    def test_stacked_cells_close_up_and_labels_push_rows(self):
        cell = _block(self.css, STACK)
        for declaration in ("margin-top: 0", "display: flow-root", "line-height: 1.5",
                            "padding: 0.125rem 0.6rem 0.125rem 8.4rem"):
            with self.subTest(declaration=declaration):
                self.assertIn(declaration, cell)
        label = _block(self.css, STACK + "::before")
        self.assertIn("position: static", label)
        self.assertIn("float: left", label)
        self.assertNotIn("position: absolute", label)
        # The label sits in the cell's left padding: 8.4 - 0.6 rem.
        self.assertIn("margin-left: -7.8rem", label)

    # 1.3
    def test_short_inline_code_never_splits(self):
        self.assertIn("white-space: pre", _block(self.css, ".sl-markdown-content :not(pre) > code.rb-short"))
        self.assertIn("white-space: break-spaces", _block(self.css, ".sl-markdown-content :not(pre) > code"))
        self.assertIn("white-space: nowrap", _block(self.css, ".sl-markdown-content table.rb-options td:first-child"))
        # A stacked value column is narrow: short code there moves down whole
        # and splits only when wider than the column, never pushing the page.
        stacked = _block(self.css, STACK + " code.rb-short")
        for declaration in ("display: inline-block", "max-width: 100%", "overflow-wrap: anywhere"):
            with self.subTest(declaration=declaration):
                self.assertIn(declaration, stacked)
        # Commands in code blocks keep wrapping on a phone.
        self.assertIn("white-space: pre-wrap", _block(self.css, ".sl-markdown-content .expressive-code pre > code"))

    # 1.4
    def test_lone_card_links_are_24px_targets(self):
        dd = _block(self.css, ".sl-markdown-content .rb-facts dd")
        self.assertIn("min-height: 24px", dd)
        self.assertIn("line-height: 24px", dd)
        link = _block(self.css, ".sl-markdown-content .rb-facts dd > a")
        self.assertIn("display: inline-block", link)
        self.assertIn("min-height: 24px", link)

    # 1.5
    def test_card_labels_fit_and_lookup_labels_match(self):
        facts = _rules(self.css, "dl.rb-facts")[0]
        self.assertIn("grid-template-columns: 11rem minmax(0, 1fr)", facts)
        self.assertIn("font-weight: 600", _block(self.css, ".sl-markdown-content .rb-file-lookup label"))


if __name__ == "__main__":
    unittest.main()
