"""Round 2 theme: the stylesheet fixes of package 1, read from its source.

Each test fails on the stylesheet as it was before the fix: a red focus ring
and hover, Starlight's 35 px h2, tinted disclosure blocks, striped asides,
the search result file dump, lifting shadows, a sidebar without hierarchy,
two indigos, permalinks hidden on touch screens, OS tabs cut at 320 px, a
column that moves on pages without a TOC.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSS_PATH = ROOT / "website" / "src" / "styles" / "retrobios.css"

# Page backgrounds Starlight 0.42.6 draws under our tokens, read with
# getComputedStyle on a built page (light: white, dark: gray-7 hsl(224 10% 10%)).
PAGE_BG = {"light": "#ffffff", "dark": "#17181c"}
# Text Starlight draws on a filled button (--sl-color-black in each theme).
ON_FILL = {"light": "#ffffff", "dark": "#17181c"}


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _blocks(css: str) -> list[tuple[str, str]]:
    """Innermost (selector, body) pairs, media queries flattened."""
    return [(sel.strip(), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]


def _theme_tokens(css: str, selector: str) -> dict[str, str]:
    for sel, body in _blocks(css):
        if sel == selector:
            return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body))
    raise AssertionError(f"no block {selector}")


def _rules(css: str, selector_part: str) -> list[str]:
    return [body for sel, body in _blocks(css) if selector_part in sel]


def _luminance(hex_colour: str) -> float:
    value = hex_colour.lstrip("#")
    channels = [int(value[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _ratio(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


class ThemeStylesheet(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.css = _strip_comments(CSS_PATH.read_text(encoding="utf-8"))
        dark = _theme_tokens(cls.css, ":root")
        light = {**dark, **_theme_tokens(cls.css, ":root[data-theme='light']")}
        cls.tokens = {"dark": dark, "light": light}

    def resolve(self, theme: str, token: str) -> str:
        value = self.tokens[theme][token].strip()
        while value.startswith("var("):
            value = self.tokens[theme][value[4:-1].strip()].strip()
        return value

    # 1.1
    def test_focus_ring_is_the_link_colour_not_red(self):
        focus = _rules(self.css, ":focus-visible")
        self.assertTrue(focus)
        for body in focus:
            self.assertIn("2px solid var(--rb-link)", body)
            self.assertNotIn("accent", body.replace("--rb-link", ""))
        ring = next(b for s, b in _blocks(self.css) if s.startswith(":where(a, button"))
        self.assertIn("outline-offset: 2px", ring)
        self.assertNotIn("#b42318", self.css)
        self.assertNotIn("--rb-accent", self.css)

    def test_button_hover_stays_in_its_own_colour(self):
        self.assertNotRegex(self.css, r"\.rb-button:is\(:hover,\s*:focus\)")
        primary = _rules(self.css, ".rb-button-primary:hover")
        self.assertTrue(primary)
        self.assertIn("var(--rb-link-strong)", primary[0])
        self.assertEqual(self.resolve("light", "--rb-link-strong"), "#1d2f73")
        self.assertEqual(self.resolve("dark", "--rb-link-strong"), "#c9d2ff")
        outline = [b for s, b in _blocks(self.css) if s.endswith(".rb-button:hover")]
        self.assertIn("var(--sl-color-accent-low)", outline[0])

    # 1.3
    def test_heading_scale(self):
        root = self.tokens["dark"]
        self.assertEqual(root["--sl-text-h1"], "2.25rem")
        self.assertEqual(root["--sl-text-h2"], "1.5rem")
        self.assertEqual(root["--sl-text-h3"], "1.2rem")
        self.assertEqual(root["--sl-text-h4"], "1rem")
        self.assertRegex(self.css, r"@media \(max-width: 50rem\) \{\s*:root \{ --sl-text-h1: 1\.875rem; \}")
        self.assertRegex(self.css, r"level-h2, h2\) \{ margin-top: 2\.75rem; \}")
        self.assertRegex(self.css, r"level-h3, h3:not\(\.rb-file-title\)\) \{ margin-top: 1\.75rem; \}")

    # 1.2
    def test_asides_are_neutral_boxes(self):
        neutral = _rules(self.css, ".starlight-aside:is(.starlight-aside--note, .starlight-aside--tip)")
        joined = "".join(neutral)
        self.assertIn("--sl-color-asides-border: var(--rb-border)", joined)
        self.assertIn("background: var(--rb-surface)", joined)
        self.assertIn("display: none", joined)
        box = _rules(self.css, ".sl-markdown-content .starlight-aside")[0]
        self.assertIn("border: 1px solid", box)

    # 1.4
    def test_disclosure_blocks_are_neutral(self):
        self.assertNotIn("rb-details-tip", self.css)
        self.assertNotIn("--sl-color-blue", self.css)
        base = next(b for s, b in _blocks(self.css) if s == ".sl-markdown-content details.rb-details")
        self.assertIn("background: transparent", base)
        self.assertIn("var(--rb-border)", base)
        self.assertIn("0.95rem", _rules(self.css, "details.rb-details > summary")[0])

    # 1.5
    def test_search_hides_the_file_list(self):
        self.assertIn("display: none", _rules(self.css, "site-search .pagefind-ui__result-tags")[0])

    # 1.6
    def test_no_lifting_shadow_on_pagination_or_menu(self):
        self.assertIn("box-shadow: none", _rules(self.css, ".pagination-links a")[0])
        self.assertIn("font-size: 1.05rem", _rules(self.css, ".pagination-links .link-title")[0])
        menu = next(b for s, b in _blocks(self.css) if s == ".sl-menu-button")
        self.assertIn("box-shadow: none", menu)
        self.assertIn("background: transparent", menu)
        self.assertIn("1px solid var(--rb-border)", menu)

    # 1.7
    def test_sidebar_hierarchy(self):
        label = _rules(self.css, ".top-level summary .large")[0]
        self.assertIn("var(--sl-text-xs)", label)
        self.assertIn("var(--rb-text-secondary)", label)
        current = _rules(self.css, "a[aria-current='page']")[0]
        self.assertIn("background: var(--sl-color-accent-low)", current)
        self.assertIn("color: var(--sl-color-accent-high)", current)
        self.assertNotIn("border", current)

    # 1.8
    def test_one_indigo(self):
        for theme in ("light", "dark"):
            with self.subTest(theme=theme):
                self.assertEqual(self.resolve(theme, "--sl-color-accent"), self.resolve(theme, "--rb-link"))
                self.assertEqual(self.resolve(theme, "--sl-color-text-accent"), self.resolve(theme, "--rb-link"))

    # 1.9
    def test_permalinks_visible_without_hover(self):
        self.assertRegex(self.css, r"@media \(hover: none\) \{\s*\.rb-permalink \{ opacity: 1; \}")
        permalink = next(b for s, b in _blocks(self.css) if s == ".rb-permalink")
        self.assertIn("min-height: 24px", permalink)

    # 1.10
    def test_os_tabs_wrap(self):
        self.assertIn("flex-wrap: wrap", _rules(self.css, "starlight-tabs [role='tablist']")[0])
        self.assertIn("white-space: nowrap", _rules(self.css, "starlight-tabs [role='tab']")[0])

    # 1.11
    def test_page_without_toc_keeps_the_column(self):
        self.assertIn("--__toc-width: initial", _rules(self.css, ":root[data-has-sidebar]:not([data-has-toc])")[0])
        pane = _rules(self.css, ":not([data-has-toc]) .main-pane")[0]
        self.assertIn("--sl-content-margin-inline: auto 0", pane)

    # 1.12, 1.14
    def test_card_pages(self):
        cards = next(b for s, b in _blocks(self.css) if s == ".sl-markdown-content :is(.rb-sys-file, .rb-file-entry)")
        self.assertIn("content-visibility: auto", cards)
        self.assertIn("contain-intrinsic-size: auto 320px", cards)
        self.assertIn("--sl-content-width: 54rem", _rules(self.css, "body:has(.rb-sys-file, .rb-file-entry)")[0])
        facts = _rules(self.css, "dl.rb-facts")[0]
        self.assertIn("grid-template-columns: 9rem minmax(0, 1fr)", facts)
        self.assertRegex(self.css, r"@media \(max-width: 30rem\) \{\s*\.sl-markdown-content dl\.rb-facts \{ grid-template-columns: minmax\(0, 1fr\)")

    # 1.13
    def test_long_values_wrap_between_words(self):
        table = _rules(self.css, ".rb-meta-card table")[0]
        self.assertIn("table-layout: fixed", table)
        self.assertIn("display: table", table)
        self.assertNotRegex(self.css, r"\.rb-meta-card td \{ overflow-wrap: anywhere")
        self.assertIn("overflow-wrap: anywhere", _rules(self.css, ".rb-meta-card td code")[0])
        self.assertIn("width: 40%", "".join(_rules(self.css, ".rb-meta-card td:first-child")))
        upstream = _rules(self.css, "#by-upstream-emulator")[0]
        self.assertIn("max-width: 22rem", upstream)

    # 1.15
    def test_two_column_tables_stack_label_over_value(self):
        self.assertIn("content: none", _rules(self.css, "table[data-rb-stack='kv'] td::before")[0])
        self.assertTrue(_rules(self.css, "table[data-rb-stack='kv'] td:first-child"))

    def test_contrast_meets_wcag_aa_in_both_themes(self):
        for theme in ("light", "dark"):
            bg = PAGE_BG[theme]
            pairs = [
                ("link", self.resolve(theme, "--rb-link"), bg, 4.5),
                ("focus ring", self.resolve(theme, "--rb-link"), bg, 3.0),
                ("primary button", ON_FILL[theme], self.resolve(theme, "--rb-link"), 4.5),
                ("primary hover", ON_FILL[theme], self.resolve(theme, "--rb-link-strong"), 4.5),
                ("outline hover", self.resolve(theme, "--rb-link"), self.resolve(theme, "--sl-color-accent-low"), 4.5),
                ("sidebar current", self.resolve(theme, "--sl-color-accent-high"), self.resolve(theme, "--sl-color-accent-low"), 4.5),
                ("secondary text", self.resolve(theme, "--rb-text-secondary"), bg, 4.5),
                ("link on surface", self.resolve(theme, "--rb-link"), self.resolve(theme, "--rb-surface"), 4.5),
            ]
            for label, fg, back, need in pairs:
                with self.subTest(theme=theme, pair=label):
                    self.assertGreaterEqual(_ratio(fg, back), need, f"{fg} on {back}")


if __name__ == "__main__":
    unittest.main()


class DeviceTilesAndHeaderLogo(unittest.TestCase):
    """The device tiles had no style once merged: every line underlined and one
    tall block per setup on a phone. The header logo was sized from the whole
    header, tab row included, and grew to 84 px."""

    def test_a_tile_is_one_compact_link(self):
        css = CSS_PATH.read_text(encoding="utf-8")
        self.assertRegex(css, r"a\.rb-tile \{[^}]*grid-template-columns: 40px 1fr")
        self.assertRegex(css, r"a\.rb-tile \{[^}]*text-decoration: none")

    def test_the_header_logo_follows_the_top_bar(self):
        css = CSS_PATH.read_text(encoding="utf-8")
        self.assertIn("header .site-title img { height: calc(4rem - 2 * var(--sl-nav-pad-y))", css)
