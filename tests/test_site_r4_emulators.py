"""Emulator pages, round 4 (package 7).

AetherSX2's page offered a command that answered "No supported platform
detected" and a file to get by one name, while the app reads any PS2 BIOS
by its contents. FinalBurn Neo and MAME 2003-Plus named no romset under
their intro. MAME's card pages and the data directory pages did not say
where they sit. melonDS marked its DSi files "required" above its DS files.
The `00_example` template was listed as an emulator.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import generate_site as gs  # noqa: E402

EMPTY_DB = {"files": {}, "indexes": {"by_name": {}}}
DIST = REPO_ROOT / "website" / "dist"


def _profile(name: str) -> dict:
    return yaml.safe_load((REPO_ROOT / "emulators" / f"{name}.yml").read_text(encoding="utf-8"))


def _pages(name: str, profile: dict | None = None) -> dict[str, str]:
    return gs.emulator_pages(name, profile or _profile(name), EMPTY_DB, None, set())


def _front(page: str) -> dict:
    assert page.startswith("---\n"), page[:80]
    return yaml.safe_load(page[4:page.index("\n---\n", 4)])


class AnAppThatReadsAnyBios(unittest.TestCase):
    def setUp(self) -> None:
        self.page = _pages("aethersx2")["aethersx2"]

    def test_the_intro_says_what_to_do(self):
        intro = self.page[self.page.index("# AetherSX2"):self.page.index("About this profile")]
        self.assertIn("AetherSX2 needs one Sony PlayStation 2 BIOS file, any region.", intro)
        # Round 5 (7.1): the folder itself, per system the app runs on.
        self.assertRegex(intro, r"\n1\. .+\n2\. Put it in this folder:")
        self.assertIn("`Android/data/xyz.aethersx2.android/files/bios/`", intro)
        self.assertIn("Any file name works. The app reads what the file holds.", intro)
        self.assertNotIn("It reads", intro)

    def test_the_card_names_an_example(self):
        self.assertIn("a Sony PlayStation 2 BIOS (example: <code>ps2-0230a-20080220.bin</code>)",
                      self.page)

    def test_no_command_that_cannot_place_the_file(self):
        # Round 5 (7.2): only through a setup that launches the app, on its
        # device, never as if the app on its own were found.
        install = self.page[self.page.index("Install this emulator's files"):]
        install = install[:install.index("</details>")]
        self.assertRegex(install, r"With [^:]+: the installer finds")
        self.assertEqual(set(re.findall(r'<TabItem label="([^"]+)">', install)), {"On the device"})
        nether = _pages("nethersx2")["nethersx2"]
        if "--emulator nethersx2" in nether:
            self.assertIn("With ", nether)

    def test_a_core_keeps_its_command(self):
        # LRPS2 runs in RetroArch, whose BIOS folder the installer finds.
        page = _pages("lrps2")["lrps2"]
        self.assertIn("--emulator lrps2", page)
        self.assertIn("2. Put it in `pcsx2/bios/`, inside your BIOS folder.", page)
        self.assertIn("a Sony PlayStation 2 BIOS, any file name", page)

    def test_only_found_by_content_with_one_bios(self):
        # DuckStation picks the BIOS of the game's region: its intro stays.
        self.assertIsNone(gs.any_name_bios(_profile("duckstation")))
        self.assertIsNone(gs.any_name_bios(_profile("melonds")))
        self.assertIsNotNone(gs.any_name_bios(_profile("pcsx2")))

    def test_the_download_links_the_collection_card(self):
        steps = gs._any_name_steps("X", _profile("aethersx2"),
                                   gs.any_name_bios(_profile("aethersx2")),
                                   "../systems/sony.md#sha1-abc")
        self.assertIn("1. Download [`ps2-0230a-20080220.bin`](../systems/sony.md#sha1-abc).", steps)


class EveryArcadeCoreNamesItsRomset(unittest.TestCase):
    def test_finalburn_and_mame_2003_plus(self):
        for name, romset in (("fbneo", f"FinalBurn Neo {_profile('fbneo')['core_version']}"),
                             ("mame2003_plus", "MAME 0.78 (plus backports)")):
            page = _pages(name)[name]
            line = page.index(f"Its BIOS and game sets must come from the {romset} romset.")
            self.assertLess(line, page.index("About this profile"), name)


class PartsKnowWhereTheySit(unittest.TestCase):
    def test_emulator_card_parts(self):
        pages = _pages("mame")
        parts = sorted((stem for stem in pages if stem.startswith("mame-files-")),
                       key=lambda stem: int(stem.rsplit("-", 1)[1]))
        self.assertGreater(len(parts), 2)
        for index, stem in enumerate(parts):
            front = _front(pages[stem])
            self.assertEqual(front["parent"], {"label": "MAME", "link": "mame.md"})
            self.assertEqual(front["searchWeight"], 0.5)
            self.assertEqual(front["prev"]["link"],
                             f"{parts[index - 1]}.md" if index else "mame.md")
            if index + 1 < len(parts):
                self.assertEqual(front["next"]["link"], f"{parts[index + 1]}.md")
            else:
                self.assertIs(front["next"], False)

    def test_data_directory_parts(self):
        ref = "x-sys"
        members = {f"f{i:05d}": {"size": 1, "sha1": f"{i:040x}"}
                   for i in range(gs.DATA_TREE_ROWS_PER_PAGE * 2 + 1)}
        gs._DATA_MEMBERS[ref] = members
        gs._DATA_READERS[ref] = [("x", "X")]
        try:
            pages = gs.data_tree_pages(ref, members)
            self.assertEqual(len(pages), 3)
            middle = _front(gs._data_tree_page(ref, {}, pages, 1))
        finally:
            del gs._DATA_MEMBERS[ref], gs._DATA_READERS[ref]
        self.assertEqual(middle["parent"], {"label": "X", "link": "x.md"})
        self.assertEqual(middle["prev"]["link"], f"{pages[0][0]}.md")
        self.assertEqual(middle["next"]["link"], f"{pages[2][0]}.md")
        self.assertEqual(middle["search"], "exclude")


class ARequiredFileSaysForWhichSystem(unittest.TestCase):
    def test_melonds_dsi_files(self):
        page = _pages("melonds")["melonds"]
        row = page[page.index('id="file-dsi-nand-bin"'):]
        row = row[:row.index("</tr>")]
        self.assertIn(">required for Nintendo DSi</span>", row)
        self.assertNotIn(">required</span>", page)

    def test_one_system_says_required(self):
        # The section heading says it: the rows under it repeat nothing.
        page = _pages("aethersx2")["aethersx2"]
        self.assertIn("## Required", page)
        self.assertNotIn("required for", page)


class TheTemplateIsNoEmulator(unittest.TestCase):
    def test_off_the_index_and_the_search(self):
        profiles = {"00_example": _profile("00_example"), "melonds": _profile("melonds")}
        index = gs.generate_emulators_index(profiles)
        self.assertNotIn("00_example", index)
        self.assertIn("melonds.md", index)
        # A test profile gets no page at all (generate_site's public_profiles).
        self.assertEqual(_profile("00_example")["type"], "test")


@unittest.skipUnless((DIST / "emulators" / "aethersx2" / "index.html").is_file(), "site not built")
class TheBuiltPages(unittest.TestCase):
    def _html(self, path: str) -> str:
        return (DIST / path / "index.html").read_text(encoding="utf-8")

    def test_aethersx2_downloads_from_the_collection(self):
        page = self._html("emulators/aethersx2")
        self.assertRegex(page, r'Download <a href="[^"]*/systems/sony[^"]*#sha1-[0-9a-f]{12}"')
        # The command only where a setup launches AetherSX2 itself (KNULLI,
        # ROCKNIX), never through RetroArch's folder, which the app never reads.
        self.assertIn("With ROCKNIX or KNULLI:", page)
        self.assertNotIn("RetroArch", page.split("Install this emulator", 1)[1].split("</details>", 1)[0])

    def test_the_index_leaves_the_template_out(self):
        # 00_example is `type: test`: no page, no index row.
        self.assertNotIn("00_example", self._html("emulators"))
        self.assertIn('data-pagefind-body', self._html("emulators/melonds"))
        self.assertFalse((DIST / "emulators" / "00_example").exists())

    def test_mame_parts_have_a_parent_crumb(self):
        page = self._html("emulators/mame-files-3")
        crumbs = re.search(r'<nav[^>]*rb-breadcrumbs.*?</nav>', page, re.S)
        self.assertIsNotNone(crumbs)
        self.assertIn(">MAME<", crumbs.group(0))


if __name__ == "__main__":
    unittest.main()
