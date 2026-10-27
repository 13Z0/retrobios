"""Round 5, package 3: search, sidebar and head.

The get/ download pages sat in no tab, and the theme then numbered the tabs
0 to 4. "snes bios" found NES.emu and never the SNES page, which answers
"No file needed" and so never says BIOS where the search reads. An emulator
crumb ran through its letter group ("Emulators / D"). The home page's
structured data named the page and called it part of itself, and an
emulator's source repository published a commit id as its version beside a
placeholder description. These tests read the generator and, where it is
built, the site.
"""

from __future__ import annotations

import ast
import json
import re
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import generate_site  # noqa: E402
import sitestarlight  # noqa: E402
from common import list_registered_platforms, load_emulator_profiles  # noqa: E402
from tests.test_site_r2_nav import ANCHORS, _nav, _present  # noqa: E402


def _gate_queries() -> dict[str, tuple[tuple[str, ...], int]]:
    """The gate's QUERIES, read from website/tools/search_gate.py: a tool
    script, not a module the suite installs."""
    tree = ast.parse((REPO_ROOT / "website" / "tools" / "search_gate.py").read_text(encoding="utf-8"))
    node = next(node for node in tree.body if isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name) and node.target.id == "QUERIES")
    return ast.literal_eval(node.value)


QUERIES = _gate_queries()
DIST = REPO_ROOT / "website" / "dist"
ROUTE = REPO_ROOT / "website" / "src" / "routeData.ts"
PLATFORMS = str(REPO_ROOT / "platforms")


def _sidebar_with_get_pages() -> tuple[dict, list[str]]:
    nav = _nav()
    names = list_registered_platforms(PLATFORMS, include_archived=True)
    get_pages = ["get/index.md", *(f"get/{name}.md" for name in names)]
    return sitestarlight.sidebar(nav, _present(nav) | set(get_pages), ANCHORS), get_pages


class GetPagesSitInYourSetup(unittest.TestCase):
    """3.1: a folder the navigation lists nothing of takes the tab of the
    pages of the same name, and nothing is excluded from every tab."""

    def test_every_get_page_joins_your_setup(self):
        side, get_pages = _sidebar_with_get_pages()
        self.assertEqual(side["options"]["exclude"], [])
        setup = set(side["options"]["topics"].get("your-setup", []))
        for rel in get_pages:
            with self.subTest(page=rel):
                self.assertIn("/" + sitestarlight._sidebar_link(rel).strip("/"), setup)

    def test_no_page_is_left_out_of_every_tab(self):
        nav = _nav()
        present = _present(nav) | {"orphans/alone.md"}
        side = sitestarlight.sidebar(nav, present, ANCHORS)
        self.assertEqual(side["options"]["exclude"], [])
        placed = {path for paths in side["options"]["topics"].values() for path in paths}
        self.assertIn("/orphans/alone", placed)


class ConsoleWordsReachTheConsole(unittest.TestCase):
    """3.2: "<name> BIOS" for each console the page names, from its own
    `consoles` field, never a word typed in the generator."""

    def test_each_console_with_bios(self):
        self.assertEqual(sitestarlight.console_words(["SNES", "Super Famicom"]),
                         ["SNES BIOS", "Super Famicom BIOS"])

    def test_the_terms_field_carries_them(self):
        _target, text = sitestarlight.finalize_page(
            "systems/nintendo-snes.md", "# SNES\n\nNo file needed.\n", level="0",
            weight=None, browser_title="SNES", description="d",
            structured={"@type": "TechArticle"}, terms={},
            authored={"consoles": ["SNES"]})
        self.assertIn('data-pagefind-meta="consoles:SNES"', text)
        self.assertIn('data-pagefind-meta="terms:SNES BIOS"', text)

    def test_no_search_word_is_typed_in_the_generator(self):
        tree = ast.parse((REPO_ROOT / "scripts" / "sitestarlight.py").read_text(encoding="utf-8"))
        docstrings = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Expr)}
        strings = {node.value.casefold() for node in ast.walk(tree)
                   if isinstance(node, ast.Constant) and isinstance(node.value, str)
                   and id(node) not in docstrings}
        typed = {text for text in strings for query in QUERIES
                 if re.search(rf"(?<!\w){re.escape(query)}(?!\w)", text)}
        self.assertEqual(typed, set())


class GateQueries(unittest.TestCase):
    """3.2, 3.3 and 3.6: the queries the gate runs, the pages that answer
    them and the rank they must reach."""

    EXPECTED = {
        "snes bios": (("systems/nintendo-snes/",), 1),
        "nes bios": (("systems/nintendo-nes/",), 1),
        "gamecube bios": (("systems/nintendo-gamecube/",), 1),
        "game boy bios": (("systems/nintendo-game-boy/",), 1),
        "3ds bios": (("systems/nintendo-3ds/",), 1),
        "check my bios folder": (("wiki/troubleshooting/",), 3),
        "is my bios correct": (("files/",), 3),
        "schema": (("data/",), 3),
        "json schema": (("data/",), 3),
        "license": (("data/", "wiki/legal/"), 3),
        "check bios folder script": (("data/",), 3),
        "ios": (("which-pack/", "wiki/faq/"), 3),
    }

    def test_the_gate_runs_each_query(self):
        for query, expected in self.EXPECTED.items():
            with self.subTest(query=query):
                self.assertEqual(QUERIES.get(query), expected)

    def test_each_console_query_names_a_generated_console_page(self):
        docs = REPO_ROOT / "website" / "src" / "content" / "docs"
        if not docs.is_dir():
            self.skipTest("site not generated")
        for query, (pages, _rank) in self.EXPECTED.items():
            if not query.endswith(" bios"):
                continue
            stem = pages[0].strip("/")
            with self.subTest(query=query):
                self.assertTrue(any((docs / f"{stem}{ext}").is_file() for ext in (".md", ".mdx")),
                                stem)


class LetterGroupsAreNoCrumb(unittest.TestCase):
    """3.4: a group of names by their first letter is a way through a
    list, not a place."""

    def test_by_initial(self):
        names = [{"label": "Dolphin", "link": "/d"}, {"label": "DuckStation", "link": "/e"}]
        self.assertTrue(sitestarlight.by_initial("D", names))
        self.assertFalse(sitestarlight.by_initial("E", names))
        digits = [{"label": "2600.emu", "link": "/a"}, {"label": "81", "link": "/b"}]
        self.assertTrue(sitestarlight.by_initial("0 to 9", digits))
        self.assertFalse(sitestarlight.by_initial("Sony", names))
        self.assertFalse(sitestarlight.by_initial("D", [{"label": "D", "items": names}]))

    def test_emulator_letter_groups_are_marked(self):
        side = sitestarlight.sidebar(_nav(), _present(_nav()), ANCHORS)
        letters = {key: value for key, value in side["groups"].items()
                   if key.startswith("Explore/Emulators/")}
        self.assertTrue(letters)
        for key, group in letters.items():
            with self.subTest(group=key):
                self.assertIs(group["crumb"], False)
        self.assertIs(side["groups"]["Explore/Emulators"]["crumb"], True)
        self.assertIs(side["groups"]["Explore/Systems"]["crumb"], True)

    def test_the_route_middleware_skips_them(self):
        self.assertIn("group?.crumb === false", ROUTE.read_text(encoding="utf-8"))


class StructuredData(unittest.TestCase):
    """3.5: the home page is the site, and a source repository states the
    release, never a commit id, and no placeholder description."""

    def test_versions_name_releases_only(self):
        release = generate_site.release_versions
        self.assertEqual(release({"core_version": "v0.1-12070"}), {"": "v0.1-12070"})
        self.assertEqual(release({"core_version": "1.5.85"}), {"": "1.5.85"})
        self.assertEqual(release({"core_version": "8dc77b0"}), {})
        self.assertEqual(release({"core_version": "0.8.139-2-g25e1ee2fd5"}), {})
        self.assertEqual(release({"core_version": "libretro branch"}), {})
        self.assertEqual(release({"core_version": "edge"}), {})
        self.assertEqual(release({}), {})
        self.assertEqual(release({"core_version": {"standalone": "v1.1", "libretro": "4122fed"}}),
                         {"standalone": "v1.1"})
        # A pin of digits only, in the version: supermodel3.
        self.assertEqual(release({"core_version": "0.3a-1481545-mm DirtBagXon (Sinden)",
                                  "upstream_commit": "14815455dc28fca2c8662fb1ed99d447ecf8f364"}),
                         {})
        # A dated release whose digits start no pin stays.
        self.assertEqual(release({"core_version": "20200712", "source_commit": "a1626f4e"}),
                         {"": "20200712"})

    def test_the_pin_is_never_a_version(self):
        data = generate_site.emulator_structured_data({
            "emulator": "Redream", "source": "https://github.com/x/redream",
            "source_commit": "0123abcd",
        })
        self.assertEqual(data["about"], {"@type": "SoftwareSourceCode", "name": "Redream",
                                         "codeRepository": "https://github.com/x/redream"})


class _Head(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ld: list[dict] = []
        self.sidebar: list[str] = []
        self.tabs_current = 0
        self._ld: list[str] | None = None
        self._label: list[str] | None = None
        self._tabs = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = dict(attrs)
        if tag == "script" and a.get("type") == "application/ld+json":
            self._ld = []
        if tag == "summary":
            self._label = []
        if tag == "nav" and a.get("aria-label") == "Sections":
            self._tabs = True
        elif self._tabs and tag == "a" and a.get("aria-current"):
            self.tabs_current += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "nav":
            self._tabs = False
        if tag == "script" and self._ld is not None:
            self.ld.append(json.loads("".join(self._ld)))
            self._ld = None
        if tag == "summary" and self._label is not None:
            self.sidebar.append(" ".join("".join(self._label).split()))
            self._label = None

    def handle_data(self, data: str) -> None:
        for buffer in (self._ld, self._label):
            if buffer is not None:
                buffer.append(data)


def _head(path: Path) -> _Head:
    page = _Head()
    page.feed(path.read_text(encoding="utf-8"))
    return page


@unittest.skipUnless((DIST / "index.html").is_file(), "site not built")
class BuiltSite(unittest.TestCase):
    def test_no_sidebar_group_is_named_by_a_number(self):
        for path in sorted(DIST.rglob("*.html")):
            labels = _head(path).sidebar
            with self.subTest(page=str(path.relative_to(DIST))):
                self.assertFalse([label for label in labels if re.fullmatch(r"\d+", label)])

    def test_every_get_page_has_one_current_tab(self):
        pages = sorted((DIST / "get").rglob("index.html"))
        self.assertTrue(pages)
        for path in [*pages, DIST / "404.html", DIST / "index.html"]:
            with self.subTest(page=str(path.relative_to(DIST))):
                self.assertEqual(_head(path).tabs_current, 1)

    def test_the_home_page_is_the_site(self):
        ld = _head(DIST / "index.html").ld
        self.assertEqual(len(ld), 1)
        self.assertEqual(ld[0]["@type"], "WebSite")
        self.assertEqual(ld[0]["name"], "RetroBIOS")
        self.assertNotIn("isPartOf", ld[0])

    def test_no_source_repository_states_a_commit_or_a_description(self):
        pins = {str(value).casefold()[:7]
                for profile in load_emulator_profiles(str(REPO_ROOT / "emulators")).values()
                for field in ("source_commit", "upstream_commit")
                for value in (profile[field].values() if isinstance(profile.get(field), dict)
                              else [profile.get(field)])
                if value}
        self.assertTrue(pins)
        seen = 0
        for path in sorted((DIST / "emulators").rglob("index.html")):
            for data in _head(path).ld:
                about = data.get("about") or []
                for entry in about if isinstance(about, list) else [about]:
                    seen += 1
                    with self.subTest(page=str(path.relative_to(DIST)), repo=entry.get("codeRepository")):
                        self.assertNotIn("description", entry)
                        runs = re.findall(r"[0-9a-f]{7,40}", str(entry.get("version", "")).casefold())
                        self.assertFalse([run for run in runs if run[:7] in pins])
                        self.assertFalse(generate_site._COMMIT_ID.search(str(entry.get("version", ""))))
        self.assertTrue(seen)


if __name__ == "__main__":
    unittest.main()
