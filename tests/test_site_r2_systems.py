"""System pages and file cards (round 2, package 6).

The Sony page opened on LibPS.exe, its card titles read "scph5501.bin
permalink" in the site search, the two setup lines of a card were defined
only in a code comment, and the systems index named no console.
"""

from __future__ import annotations

import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import generate_site as gs  # noqa: E402
import siterender  # noqa: E402
from cross_reference import FileConsumers  # noqa: E402

DIST = REPO_ROOT / "website" / "dist"


def _file(name: str, char: str, path: str = "") -> dict:
    return {"name": name, "sha1": char * 40, "md5": char * 32, "size": 524288,
            "path": path or f"bios/Sony/PlayStation/{name}"}


def _consumers(readers: dict[str, list[dict]], platforms: dict[str, set[str]] | None = None
               ) -> types.SimpleNamespace:
    """Consumers where each sha1 is read by the given profile entries, one
    emulator per entry."""
    files_of: dict[str, list] = {}
    entries: dict[str, dict[str, int]] = {}
    emulators: dict[str, set[str]] = {}
    for sha1, read_by in readers.items():
        for number, entry in enumerate(read_by):
            emulator = f"emu{number}-{sha1[:4]}"
            files_of[emulator] = [entry]
            entries.setdefault(sha1, {})[emulator] = 0
            emulators.setdefault(sha1, set()).add(emulator)
    return types.SimpleNamespace(
        platforms=platforms or {}, emulators=emulators, emulator_entries=entries,
        emulator_files=files_of, installed={}, platform_systems={},
        platform_destinations={},
    )


class TheConsoleBiosComesFirst(unittest.TestCase):
    """Cards were sorted by byte order: capitals first, so LibPS.exe and
    PSX_DTLH2000.bin opened the PlayStation section and scph5501.bin was
    28th."""

    def setUp(self):
        self.files = [
            _file("LibPS.exe", "1"), _file("PSX_DTLH2000.bin", "2"),
            _file("dtlh1100.bin", "3"), _file("scph5501.bin", "4"),
            _file("scph5500.bin", "5"), _file("scph1001.bin", "6"),
            _file("openbios.bin", "7"),
        ]
        self.consumers = _consumers({
            "4" * 40: [{"name": "scph5501.bin", "required": True, "priority": 2,
                        "region": ["north-america"]}],
            "5" * 40: [{"name": "scph5500.bin", "required": True, "priority": 5,
                        "region": ["japan"]}],
            "6" * 40: [{"name": "scph1001.bin", "required": False, "priority": 1}],
            "2" * 40: [{"name": "PSX_DTLH2000.bin", "required": False}],
        }, platforms={"6" * 40: {"a", "b"}, "3" * 40: {"a", "b", "c"}})

    def names(self) -> list[str]:
        cards = gs._console_cards("Sony", self.files, self.consumers, set())
        return [re.search(r"<code>(.*?)</code>", card[2])[1] for _v, card, _s in cards]

    def test_required_then_priority_then_setups_then_name(self):
        self.assertEqual(self.names(), [
            "scph5501.bin", "scph5500.bin",  # required, by priority
            "scph1001.bin",                 # optional with a priority
            "dtlh1100.bin",                 # listed by three setups
            "LibPS.exe", "openbios.bin", "PSX_DTLH2000.bin",  # by name, any case
        ])

    def test_a_console_cut_in_parts_goes_by_name(self):
        cards = gs._console_cards("Sony", self.files, self.consumers, set(), by_name=True)
        names = [re.search(r"<code>(.*?)</code>", card[2])[1] for _v, card, _s in cards]
        self.assertEqual(names, sorted(names, key=str.casefold))

    def test_the_section_opens_on_the_bios_by_region(self):
        lines = gs._start_with(self.files, self.consumers)
        self.assertEqual(lines[0], "**Start with** the BIOS for the region of your games:")
        self.assertIn("- **North America**: `scph5501.bin`", lines)
        self.assertIn("- **Japan**: `scph5500.bin`", lines)
        self.assertLess(lines.index("- **North America**: `scph5501.bin`"),
                        lines.index("- **Japan**: `scph5500.bin`"))

    def test_regions_needing_the_same_file_share_a_line(self):
        consumers = _consumers({"4" * 40: [
            {"required": True, "region": ["north-america", "latin-america", "brazil"]}]})
        lines = gs._start_with([_file("scph5501.bin", "4")], consumers)
        # Brazil says nothing Latin America does not.
        self.assertIn("- **North America, Latin America**: `scph5501.bin`", lines)

    def test_a_long_list_is_counted(self):
        files = [_file(f"rom{i}.bin", "abcdefgh"[i]) for i in range(8)]
        consumers = _consumers({f["sha1"]: [{"required": True}] for f in files})
        self.assertEqual(gs._start_with(files, consumers)[0],
                         "**Start with** the 8 files an emulator requires. They come first below.")

    def test_no_required_file_no_line(self):
        self.assertEqual(gs._start_with([_file("a.bin", "a")], _consumers({})), [])

    def test_the_page_carries_the_line_under_the_console(self):
        page = gs.generate_system_page("Sony", {"PlayStation": self.files}, self.consumers)
        heading = page.index("## PlayStation")
        self.assertLess(heading, page.index("**Start with**"))
        self.assertLess(page.index("**Start with**"), page.index('class="rb-sys-file"'))


class ThePermalinkIsAnIcon(unittest.TestCase):
    """The word "permalink" was link text inside every card title: the
    search showed "scph5501.bin permalink" and screen readers read it."""

    def test_no_word_and_out_of_the_search(self):
        lines = gs._system_file_card(_file("scph5501.bin", "4"), FileConsumers({"files": {}}))
        title = lines[2]
        link = re.search(r'<a class="rb-permalink"[^>]*>(.*?)</a>', title)
        self.assertNotIn("permalink", re.sub(r"<[^>]+>", "", title))
        self.assertIn('aria-label="Permanent link to scph5501.bin"', link[0])
        self.assertIn("data-pagefind-ignore", link[0])
        self.assertIn('<use href="#rb-link-icon"/>', link[1])

    def test_the_page_draws_the_icon_once(self):
        page = gs.generate_system_page(
            "Sony", {"PlayStation": [_file("a.bin", "a"), _file("b.bin", "b")]},
            FileConsumers({"files": {}}))
        self.assertEqual(page.count('<symbol id="rb-link-icon"'), 1)
        self.assertEqual(page.count('href="#rb-link-icon"'), 2)


class TheCardLinesSayWhatTheyMean(unittest.TestCase):
    """"Platforms:" and "Also installed by:" listed different setups, as
    lower-case ids, and only a code comment told them apart."""

    def setUp(self):
        sha1 = "4" * 40
        self.consumers = types.SimpleNamespace(
            platforms={sha1: {"batocera"}}, emulators={},
            platform_systems={sha1: {"batocera": {"sony-playstation"}}},
            platform_destinations={sha1: {"batocera": {"scph5501.bin"}}},
            installed={sha1: {"retrobat": {"bios/scph5501.bin"}}},
        )
        self.lines = gs._system_file_card(_file("scph5501.bin", "4"), self.consumers)

    def test_labels_and_display_names(self):
        text = "\n".join(self.lines)
        self.assertIn("<dt data-pagefind-ignore>On the setup's list</dt><dd><a href=\"../"
                      "platforms/batocera.md#sony-playstation\">Batocera</a>: scph5501.bin</dd>",
                      text)
        self.assertIn("<dt>Packed for its emulators by</dt><dd><a href=\"../platforms/"
                      "retrobat.md\">RetroBat</a>: bios/scph5501.bin</dd>", text)
        self.assertNotIn("Platforms:", text)
        self.assertNotIn("Also installed by", text)

    def test_the_setup_list_is_searched_and_the_rest_is_not(self):
        lists = [line for line in self.lines if line.startswith("<dl")]
        self.assertEqual(lists, ['<dl class="rb-facts">', '<dl class="rb-facts" data-pagefind-ignore>'])
        searched = "\n".join(self.lines).split('<dl class="rb-facts">', 1)[1].split("</dl>")[0]
        self.assertIn("On the setup's list", searched)
        self.assertNotIn("SHA1", searched)
        # The label stays out of the excerpt: the search ran the two
        # together, "On the setup's listAmberELEC".
        self.assertIn("<dt data-pagefind-ignore>On the setup's list</dt><dd>", searched)

    def test_the_page_defines_both_lines(self):
        page = gs.generate_system_page("Sony", {"PlayStation": [_file("scph5501.bin", "4")]},
                                       self.consumers)
        key = page.index("**On the setup's list** names")
        self.assertLess(key, page.index('class="rb-sys-file"'))
        self.assertIn("**Packed for its emulators by** names", page)

    def test_a_file_nothing_reads_says_so(self):
        title = gs._system_file_card(_file("GameIndex.yaml", "9"), FileConsumers({"files": {}}))[2]
        self.assertIn(">No reader</span>", title)
        self.assertNotIn("No reader", self.lines[2])


class FileFactsAreAGrid(unittest.TestCase):
    """Each card was two bullet lists of code chips."""

    def test_one_value_cell_per_label_and_plain_hashes(self):
        entry = {**_file("scph5501.bin", "4"), "crc32": "8d8cb7e4",
                 "provenance": {"redump": {"name": "ps-30a.bin"},
                                "tosec": {"name": "SCPH-5501 (1996).bin"}}}
        lines = gs._system_file_card(entry, FileConsumers({"files": {}}))
        facts = [line for line in lines if line.startswith("<dt>")]
        self.assertTrue(facts)
        for line in facts:
            self.assertEqual(line.count("<dt>"), 1, line)
            self.assertEqual(line.count("<dd>"), 1, line)
        text = "\n".join(lines)
        self.assertIn('<dt>SHA1</dt><dd><span class="rb-hash">' + "4" * 40 + "</span></dd>", text)
        self.assertNotIn("<code class=\"rb-hash\">", text)
        catalogued = next(line for line in facts if line.startswith("<dt>Catalogued"))
        self.assertIn("<br>", catalogued)
        self.assertNotRegex(text, r"(?m)^- ")

    def test_collection_path_keeps_its_chip(self):
        text = "\n".join(gs._system_file_card(_file("a.bin", "a"), FileConsumers({"files": {}})))
        self.assertIn("<dt>Collection path</dt><dd><code>bios/Sony/PlayStation/a.bin</code>", text)


class ConsoleHeadingsStartWithTheName(unittest.TestCase):
    """The icon and a space came before the name: "Section titled
    “ PlayStation”" on five of seven Sony headings."""

    def test_the_icon_is_hidden_and_touches_the_name(self):
        icon = siterender._icon_name("Sony", "PlayStation")
        with mock.patch.dict(siterender._icon_available, {icon: True}):
            heading = gs._console_heading("Sony", "PlayStation")
        self.assertRegex(heading, r'^## <img [^>]*aria-hidden="true"[^>]*>PlayStation \{ #playstation \}$')
        self.assertIn('alt=""', heading)

    def test_no_icon_no_image(self):
        icon = siterender._icon_name("Nobody", "Nothing")
        with mock.patch.dict(siterender._icon_available, {icon: False}):
            self.assertEqual(gs._console_heading("Nobody", "Nothing"), "## Nothing { #nothing }")


class TheSystemsIndexListsConsoles(unittest.TestCase):
    """The index listed 74 manufacturers and no console: PlayStation had to
    be known to be under Sony."""

    def test_one_row_per_console_by_its_name(self):
        groups = {
            "Sony": {"PlayStation": [_file("a.bin", "a")], "PlayStation 2": [_file("b.bin", "b")]},
            "Other": {"ScummVM": [_file("c.bin", "c")]},
            "Game Engines": {"TheXTech": [_file("d.png", "d")]},
        }
        page = gs.generate_systems_index(groups)
        self.assertIn("| [Sony PlayStation](sony.md#playstation) | 1 | [Sony](sony.md) |", page)
        self.assertIn("| [Sony PlayStation 2](sony.md#playstation-2) | 1 | [Sony](sony.md) |",
                      page)
        # A program's folder under Other is named alone, in its own table.
        consoles = page.split("## Emulator folders")[0]
        self.assertNotIn("ScummVM", consoles)
        self.assertIn("| [ScummVM](other.md#scummvm) | 1 | [Other](other.md) |",
                      page.split("## Emulator folders")[1])
        self.assertNotIn("TheXTech", consoles)
        self.assertIn("| [TheXTech](game-engines.md#thextech)", page)

    def test_rows_go_by_name_not_by_maker(self):
        groups = {"Sega": {"Saturn": [_file("a.bin", "a")]},
                  "Atari": {"Lynx": [_file("b.bin", "b")]}}
        page = gs.generate_systems_index(groups)
        self.assertLess(page.index("Atari Lynx"), page.index("Sega Saturn"))


class FindAFileSaysWhatAHashIs(unittest.TestCase):

    def test_the_sentence_above_the_box(self):
        page = gs.generate_files_page()
        intro = page.split('<form class="rb-file-lookup"')[0]
        self.assertIn("A hash is a fingerprint of a file's contents", intro)
        self.assertIn("(../wiki/glossary.md)", intro)
        self.assertNotIn("**permalink**", page)


@unittest.skipUnless((DIST / "systems" / "sony" / "index.html").is_file(), "site not built")
class TheBuiltSonyPage(unittest.TestCase):

    def setUp(self):
        self.text = (DIST / "systems" / "sony" / "index.html").read_text(encoding="utf-8")

    def test_no_card_title_says_permalink(self):
        titles = re.findall(r'<h3[^>]*class="rb-file-title".*?</h3>', self.text, re.S)
        self.assertTrue(titles)
        for title in titles:
            self.assertNotIn("permalink", re.sub(r"<[^>]+>", "", title))

    def test_playstation_opens_on_a_bios_it_needs(self):
        section = self.text.split('id="playstation"', 1)[1].split("<h2", 1)[0]
        self.assertIn("Start with", section.split('class="rb-sys-file"', 1)[0])
        # A console with a page of its own opens there.
        own = DIST / "systems" / "sony-playstation" / "index.html"
        if 'class="rb-file-title"' not in section and own.is_file():
            section = own.read_text(encoding="utf-8")
        first = re.search(r'class="rb-file-title"[^>]*><code>(.*?)</code>', section)[1]
        self.assertNotIn(first, {"LibPS.exe", "PSX_DTLH2000.bin"})
        self.assertRegex(first, r"^scph\d+")

    def test_headings_have_no_leading_space(self):
        self.assertNotRegex(self.text, r'<h2 id="playstation[^"]*"><img[^>]*>\s')


if __name__ == "__main__":
    unittest.main()
