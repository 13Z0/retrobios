"""Renderings shared by the site pages, and the icon cache they read.

No network: icon checks are answered by a stub.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import siterender  # noqa: E402


class _Served:
    status = 200

    def __enter__(self) -> "_Served":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class IconCacheIsReplacedWhole(unittest.TestCase):
    """The cache was truncated then dumped in place: a concurrent reader saw
    half a file, took it for empty, and an offline run then wrote ``{}``
    over every recorded icon."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "system_icons.json"
        patcher = mock.patch.object(siterender, "ICON_CACHE_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        saved = dict(siterender._icon_available)
        self.addCleanup(lambda: (siterender._icon_available.clear(),
                                 siterender._icon_available.update(saved)))

    def test_an_offline_run_leaves_an_unreadable_cache_untouched(self):
        self.path.write_text('{"Sega - Mega Drive": tr')
        offline = mock.patch.object(
            siterender.urllib.request, "urlopen",
            side_effect=urllib.error.URLError("offline"))
        with offline, mock.patch("sys.stderr"), mock.patch("builtins.print"):
            siterender.prime_system_icons({"Sega - Mega Drive"})
        self.assertEqual(self.path.read_text(), '{"Sega - Mega Drive": tr')

    def test_names_another_run_recorded_meanwhile_are_kept(self):
        self.path.write_text(json.dumps({"A - One": True}))

        def answer(*_args: object, **_kwargs: object) -> _Served:
            # A second build records its own check while this one runs.
            self.path.write_text(json.dumps({"A - One": True, "C - Three": False}))
            return _Served()

        with mock.patch.object(siterender.urllib.request, "urlopen", side_effect=answer), \
                mock.patch("builtins.print"):
            siterender.prime_system_icons({"A - One", "B - Two"})
        self.assertEqual(json.loads(self.path.read_text()),
                         {"A - One": True, "B - Two": True, "C - Three": False})
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()),
                         ["system_icons.json"])

    def test_the_write_goes_through_a_scratch_file(self):
        with mock.patch.object(siterender.urllib.request, "urlopen",
                               return_value=_Served()), \
                mock.patch.object(siterender, "write_text_atomic",
                                  wraps=siterender.write_text_atomic) as atomic, \
                mock.patch("builtins.print"):
            siterender.prime_system_icons({"B - Two"})
        atomic.assert_called_once()


class EveryPageAddressesPlatformsAsPeers(unittest.TestCase):
    """The exports page was held to neutral wording, and the gaps page next
    to it still called each platform's file a "corrected list". Reads the
    text every site generator writes, docstrings and bare identifiers aside."""

    WORDS = ("their file", "theirs", "of ours", "corrected", "left out", "drift")
    SOURCES = ("generate_site.py", "siterender.py", "sitesources.py", "siteexports.py")

    def test_no_page_text_judges_another_project(self):
        import ast
        import re

        found = []
        for name in self.SOURCES:
            tree = ast.parse((REPO_ROOT / "scripts" / name).read_text(encoding="utf-8"))
            docstrings = {
                id(node.body[0].value) for node in ast.walk(tree)
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                     ast.AsyncFunctionDef))
                and ast.get_docstring(node, clean=False) is not None
            }
            for node in ast.walk(tree):
                if (not isinstance(node, ast.Constant) or not isinstance(node.value, str)
                        or id(node) in docstrings or re.fullmatch(r"\w+", node.value)):
                    continue
                text = node.value.lower()
                found += [f"{name}:{node.lineno} {word!r}"
                          for word in self.WORDS if word in text]
        self.assertEqual(found, [])


class BundledFilesAreNotToSource(unittest.TestCase):
    """The gaps page summed the report's gap_missing, bundled files included:
    1,356 files to source where 609 were, and a red badge of 13 on ZQuest
    Classic, whose 13 missing files all come with its own package."""

    def test_the_summary_and_the_row_count_only_what_must_be_found(self):
        import generate_site

        gap = {"required": True, "source_ref": "x", "in_repo": False}
        report = {"zq": {
            "emulator": "ZQuest Classic", "gaps": 4, "gap_missing": 3, "gap_bios": 1,
            "unsourceable": [],
            "gap_details": [
                {**gap, "name": "a.dll", "source": "missing", "bundled": True},
                {**gap, "name": "b.dll", "source": "missing", "bundled": True},
                {**gap, "name": "c.bin", "source": "missing"},
                {**gap, "name": "d.bin", "source": "bios", "in_repo": True},
            ],
        }}
        page = generate_site.generate_gap_analysis(
            {"zq": {"emulator": "ZQuest Classic", "files": []}}, {},
            {"files": {}, "indexes": {}}, gap_report=report)
        self.assertIn("2 installed with their emulator, 1 to source.", page)
        self.assertIn("| missing | 1 | Not available, needs sourcing |", page)
        self.assertIn("| [ZQuest Classic](emulators/zq.md) | 4 | 1 | 0 | 0 | 2 | "
                      '<span class="rb-badge rb-badge-danger">1</span> |', page)


class StructuredBlocks(unittest.TestCase):
    PIN = "1e0de94dc7e669c0925a22c0fccf6cdc837af0a0"

    def profile(self, block: str, value: object) -> dict:
        return {"source": "https://github.com/libretro/blastem",
                "source_commit": self.PIN, block: value}

    def render(self, profile: dict) -> str:
        import generate_site

        return "\n".join(generate_site._render_structured_blocks(profile))

    def test_a_source_ref_in_a_block_is_a_pinned_link(self):
        """186 citations in analysis and firmware blocks were plain code."""
        page = self.render(self.profile("analysis", {"tmss": {
            "source_ref": "libblastem.c:355-359"}}))
        self.assertIn(
            f"[`libblastem.c:355-359`](https://github.com/libretro/blastem/blob/"
            f"{self.PIN}/libblastem.c#L355-L359)", page)

    def test_a_list_inside_a_list_item_is_shown(self):
        """cemu's firmware titles lost the files each title carries."""
        page = self.render(self.profile("firmware_titles", [
            {"id": "0005001b-10042400", "name": "Shared fonts",
             "content": ["CafeCn.ttf", "CafeKr.ttf"]}]))
        self.assertIn("    - id: 0005001b-10042400, name: Shared fonts\n", page)
        self.assertIn("        - **content:**", page)
        self.assertIn("            - CafeKr.ttf", page)

    def test_a_folded_note_stays_inside_its_block(self):
        page = self.render(self.profile("analysis", {"tmss": {
            "note": "The core points\nsystem/tmss_path at the system directory."}}))
        self.assertIn("- **note:** The core points system/tmss_path at the", page)
        body = page.split("\n")[1:]
        self.assertEqual([line for line in body if line and not line.startswith("    ")], [])

    def test_a_long_note_is_not_cut(self):
        note = "word " * 80 + "end."
        page = self.render(self.profile("analysis", {"x": {"note": note}}))
        self.assertIn("end.", page)


class FileCardsLinkTheReadingEntry(unittest.TestCase):
    def test_an_emulator_link_lands_on_its_entry(self):
        """The platform links landed on their section, the emulator links on
        the top of a page of a hundred entries."""
        import generate_site
        from cross_reference import FileConsumers

        consumers = FileConsumers({"files": {}})
        files = [{"name": "scph1001.bin"}, {"name": "scph5501.bin"},
                 {"name": "tmss.md", "sha1": "d0fd0c16" + "0" * 32},
                 {"name": "tmss.md", "sha1": "aa" * 20}]
        consumers.emulators = {"s": {"duckstation", "other"}, "t": {"blastem"}}
        consumers.emulator_entries = {"s": {"duckstation": 1}, "t": {"blastem": 2}}
        consumers.emulator_files = {"duckstation": files, "blastem": files}
        self.assertEqual(generate_site._emulator_links("s", consumers), [
            "[duckstation](../emulators/duckstation.md#file-scph5501-bin)",
            "[other](../emulators/other.md)",
        ])
        self.assertEqual(generate_site._emulator_links("t", consumers),
                         ["[blastem](../emulators/blastem.md#file-tmss-md-d0fd0c16)"])


class DirectoryMembersNameTheirEmulator(unittest.TestCase):
    """The site resolved a ``type: directory`` entry by its own name: 11,976
    files under alephone's Plugins/, cdogs' and thextech's trees had no
    emulator on their card, and the directory card linked none of them."""

    SHA1 = "c63078a6c9ccaca7339a726a5d39410983933070"

    @classmethod
    def setUpClass(cls) -> None:
        from common import build_zip_contents_index, load_database, load_emulator_profiles
        from cross_reference import FileConsumers

        cls.db = load_database(str(REPO_ROOT / "database.json"))
        if cls.SHA1 not in cls.db.get("files", {}):
            raise unittest.SkipTest("the collection does not hold the alephone plugin")
        cls.profile = load_emulator_profiles(str(REPO_ROOT / "emulators"))["alephone"]
        cls.consumers = FileConsumers(cls.db, build_zip_contents_index(cls.db))
        cls.consumers.add_emulators({"alephone": cls.profile})

    def test_a_member_names_the_emulator_and_its_directory_entry(self):
        import generate_site

        files = self.profile["files"]
        index = self.consumers.emulator_entries[self.SHA1]["alephone"]
        self.assertEqual(files[index].get("type"), "directory")
        self.assertEqual(generate_site._emulator_links(self.SHA1, self.consumers),
                         ["[alephone](../emulators/alephone.md#file-marathon-plugins)"])

    def test_the_directory_card_points_at_its_copies(self):
        import generate_site

        entry = next(f for f in self.profile["files"]
                     if f.get("type") == "directory" and f.get("path") == "Marathon/Plugins/")
        line = generate_site._directory_copies(entry, self.consumers)[0]
        self.assertRegex(line, r"^Collection copies: \d+ files under "
                               r"\[`bios/Bungie/Marathon/Plugins/`\]\(\.\./systems/bungie\.md#")


class AnAliasTreeIsListedOnItsParentsPage(unittest.TestCase):
    """The 90 FreeIntv overlays were linked to the page of the alias that
    declares them, which is never generated: every permalink was a 404."""

    PROFILES = {
        "freeintv": {"type": "libretro"},
        "freeintv_ts_overlay": {
            "type": "alias", "alias_of": "freeintv",
            "data_directories": [{"ref": "freeintv-overlays",
                                  "destination": "freeintv_overlays"}],
        },
    }

    def test_the_permalink_goes_to_a_page_that_exists(self):
        import generate_site

        members = {"freeintv-overlays": {"a.png": {"sha1": "02" * 20}}}
        items = generate_site.file_location_items({"files": {}}, self.PROFILES, members)
        self.assertEqual(items, [{"sha1": "02" * 20,
                                  "url": "emulators/freeintv/#sha1-020202020202"}])

    def test_the_parent_page_lists_the_tree(self):
        import generate_site

        trees = generate_site.page_data_directories("freeintv", self.PROFILES)
        self.assertEqual([t["ref"] for t in trees], ["freeintv-overlays"])


if __name__ == "__main__":
    unittest.main()
