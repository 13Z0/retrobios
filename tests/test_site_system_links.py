"""A platform system links to the page of the folder its files live in.

The site mapped each system by the bare name of its first file, the last
console written winning: MiSTer's boot.rom sits under a dozen consoles, so
wonderswan and tiger-game-com linked to VideoBrain, ngpc and tandy-coco to
TI-99. Reads the repository: skips when the database is absent.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import generate_site as gs  # noqa: E402


class LinksFollowContent(unittest.TestCase):
    def test_an_ambiguous_name_places_nothing(self):
        names = {"boot.rom": {("VideoBrain", "VideoBrain"), ("Bandai", "WonderSwan")}}
        files = [{"name": "boot.rom", "sha1": "0" * 40}]
        self.assertIsNone(gs._system_folder(files, {"files": {}, "indexes": {}}, names))
        names["only.rom"] = {("Bandai", "WonderSwan")}
        files.append({"name": "only.rom"})
        self.assertEqual(
            gs._system_folder(files, {"files": {}, "indexes": {}}, names),
            ("Bandai", "WonderSwan"),
        )

    def test_mister_systems(self):
        if not (REPO_ROOT / "database.json").exists():
            self.skipTest("database.json is not built")
        previous = os.getcwd()
        os.chdir(REPO_ROOT)
        self.addCleanup(os.chdir, previous)
        from common import load_database, load_platform_config

        db = load_database("database.json")
        config = load_platform_config("misterfpga")
        names: dict = {}
        for mfr, consoles in gs._group_by_manufacturer(db).items():
            for console, entries in consoles.items():
                for entry in entries:
                    names.setdefault(entry["name"], set()).add((mfr, console))
        expected = {"wonderswan": "WonderSwan", "ngpc": "Neo Geo Pocket"}
        for sys_id, console in expected.items():
            system = config["systems"].get(sys_id)
            if system is None:
                continue
            with self.subTest(system=sys_id):
                place = gs._system_folder(system.get("files", []), db, names)
                self.assertEqual(place and place[1], console)


class OtherSpellingsOfOneSystem(unittest.TestCase):
    """Platforms and profiles spell systems their own way: the RetroArch page
    listed no emulator for sega-mega-drive although genesis_plus_gx declares
    sega-megadrive, and 2,057 of 4,215 emulator system lines had no link."""

    def test_a_platform_system_finds_its_emulators(self):
        index = gs._build_emulator_file_index({
            "genesis_plus_gx": {"systems": ["sega-megadrive"]},
            "np2kai": {"systems": ["pc-98"]},
        })
        self.assertEqual(gs._system_emulators("sega-mega-drive", index), ["genesis_plus_gx"])
        self.assertEqual(gs._system_emulators("nintendo-snes", index), [])

    def _install(self, mapping: dict) -> None:
        import siterender

        saved = dict(siterender._system_page_map)
        self.addCleanup(siterender.set_system_page_map, saved)
        siterender.set_system_page_map(mapping)

    def test_a_profile_spelling_links_to_the_platform_page(self):
        import siterender

        self._install({"sega-mega-drive": ("sega", "Mega Drive")})
        self.assertIn("systems/sega.md#mega-drive", siterender._system_link("sega-megadrive"))
        self._install({"sega-mega-drive": ("sega", "Mega Drive"),
                       "megadrive": ("other", "Elsewhere")})
        self.assertEqual(siterender._system_link("sega-megadrive"), "sega-megadrive")

    def test_a_table_of_the_same_size_replaces_the_other_spellings(self):
        """The index of other spellings was kept while the table kept its
        length, so a second table of one entry linked to the first one's page."""
        import siterender

        self._install({"sega-mega-drive": ("sega", "Mega Drive")})
        self.assertIn("systems/sega.md#", siterender._system_link("sega-megadrive"))
        self._install({"sega-mega-drive": ("other", "Elsewhere")})
        self.assertEqual(siterender._system_link("sega-megadrive"),
                         "[sega-megadrive](systems/other.md#elsewhere)")

    def test_a_system_only_profiles_name_is_placed_by_their_files(self):
        manufacturers = {"Commodore": {"PET": [{"name": "basic4.bin"}]}}
        profiles = {"vice_xpet": {"systems": ["commodore-pet"],
                                  "files": [{"name": "basic4.bin"}]}}
        pages = gs._system_pages_from_data(
            manufacturers, {}, {"files": {}, "indexes": {}}, profiles)
        self.assertEqual(pages["commodore-pet"], ("commodore", "PET"))


class ACopyIsOnItsOwnFoldersPage(unittest.TestCase):
    """The database keeps one path per content: the Channel F BIOS was on the
    Arcade page only, and Fairchild had no system page at all."""

    def test_every_folder_holding_a_copy_lists_it(self):
        import tempfile

        with tempfile.TemporaryDirectory(dir=REPO_ROOT / "tmp") as tmp:
            for rel in ("Arcade/FBNeo/sl31253.rom", "Fairchild/Channel F/sl31253.bin"):
                (Path(tmp) / rel).parent.mkdir(parents=True, exist_ok=True)
                (Path(tmp) / rel).write_bytes(b"x")
            db = {
                "bios_dir": tmp,
                "files": {"s": {"path": "bios/Arcade/FBNeo/sl31253.rom", "sha1": "s",
                                "name": "sl31253.rom"}},
                "indexes": {"by_path_suffix": {
                    "Arcade/FBNeo/sl31253.rom": ["s"], "FBNeo/sl31253.rom": ["s"],
                    "Fairchild/Channel F/sl31253.bin": ["s"], "Channel F/sl31253.bin": ["s"],
                }},
            }
            groups = gs._group_by_manufacturer(db)
        self.assertEqual(sorted(groups), ["Arcade", "Fairchild"])
        self.assertEqual(list(groups["Fairchild"]), ["Channel F"])
        # The copy is listed twice and counted once.
        self.assertRegex(gs.generate_systems_index(groups), r"2 consoles, 1 files? ")


class AnIdNamesOneCardPerPage(unittest.TestCase):
    """A copy filed under two consoles of one maker was listed twice with
    the same id, and the deployed site refused 20 pages."""

    def test_the_second_card_has_no_id(self):
        from cross_reference import FileConsumers

        entry = {"name": "a.bin", "sha1": "a" * 40, "md5": "b" * 32, "size": 3,
                 "path": "bios/Nintendo/DS/a.bin"}
        page = gs.generate_system_page(
            "Nintendo", {"DS": [entry], "DSi": [entry]}, FileConsumers({"files": {}}))
        self.assertEqual(page.count("#sha1-aaaaaaaaaaaa .rb-file-title"), 1)
        self.assertEqual(page.count("rb-sys-file"), 2)


class SystemEmulatorsWhenNoIdMatches(unittest.TestCase):
    """RetroArch files NAOMI and Atomiswave as sega-dreamcast-arcade and the
    Enterprise as enterprise-64-128, ids no profile uses: the emulator cell
    of both rows was empty on every platform page."""

    def setUp(self):
        self.index = gs._build_emulator_file_index({
            "flycast": {"systems": ["sega-naomi"], "files": [{"name": "airlbios.zip"}]},
            "fbneo": {"systems": ["arcade"], "files": [
                {"name": "boot.bin", "archive": "bubsys.zip"}]},
            "genesis": {"systems": ["sega-mega-drive"], "files": [{"name": "bios_MD.bin"}]},
        })

    def test_a_file_the_system_lists_names_its_emulator(self):
        self.assertEqual(
            gs._system_emulators("sega-dreamcast-arcade", self.index, [{"name": "airlbios.zip"}]),
            ["flycast"],
        )

    def test_an_archive_member_is_not_a_file_by_that_name(self):
        self.assertEqual(
            gs._system_emulators("sega-dreamcast", self.index, [{"name": "boot.bin"}]), []
        )

    def test_an_id_match_is_kept_alone(self):
        self.assertEqual(
            gs._system_emulators("sega-megadrive", self.index, [{"name": "airlbios.zip"}]),
            ["genesis"],
        )


if __name__ == "__main__":
    unittest.main()
