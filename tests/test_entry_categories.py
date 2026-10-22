"""What a platform entry is: firmware, a game's data, or unread.

Strict verification fails on firmware gaps only, so the category has to come
from what the cores the platform runs open, never from the entry's name.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import verify  # noqa: E402
from exporter.candidates import BIOS, GAME_DATA, UNREAD, EntryCategories  # noqa: E402

PROFILES = {
    "bluemsx": {
        "emulator": "blueMSX", "type": "libretro", "cores": ["bluemsx"],
        "systems": ["microsoft-msx"],
        "files": [{"name": "MSX.ROM", "path": "Machines/Shared Roms/MSX.ROM"}],
    },
    "devilutionx": {
        "emulator": "DevilutionX", "type": "game", "cores": ["devilutionx"],
        "systems": ["diablo"],
        "files": [{"name": "diabdat.mpq", "category": "game_data",
                   "source_ref": "Source/init.cpp:120"}],
    },
}


def _config(cores: list[str], files: list[dict], system: str = "s") -> dict:
    return {"platform": "P", "verification_mode": "md5", "cores": cores,
            "systems": {system: {"files": files}}}


class Category(unittest.TestCase):
    def test_each_category(self):
        cats = EntryCategories(_config(["bluemsx", "devilutionx"], []), PROFILES)
        self.assertEqual(cats.category({"name": "MSX.ROM"}, "bios/Machines/MSX/MSX.ROM", "microsoft-msx"), BIOS)
        self.assertEqual(cats.category({"name": "diabdat.mpq"}, "ports/diablo/diabdat.mpq", "diablo"),
                         GAME_DATA)
        self.assertEqual(
            cats.category({"name": "hardwareconfig.xml"}, "bios/Machines/MSX/hardwareconfig.xml", "microsoft-msx"),
            UNREAD)

    def test_a_core_without_profile_leaves_unread_open(self):
        cats = EntryCategories(_config(["bluemsx", "devilutionx", "mystery"], []), PROFILES)
        self.assertEqual(
            cats.category({"name": "hardwareconfig.xml"}, "bios/Machines/MSX/hardwareconfig.xml", "microsoft-msx"),
            BIOS)
        self.assertEqual(cats.category({"name": "diabdat.mpq"}, "ports/diablo/diabdat.mpq", "diablo"),
                         GAME_DATA)

    def test_a_system_no_examined_core_emulates_stays_firmware(self):
        cats = EntryCategories(_config(["bluemsx", "devilutionx"], []), PROFILES)
        self.assertEqual(cats.category({"name": "syscard3.pce"}, "syscard3.pce", "nec-pc-engine"),
                         BIOS)

    def test_cores_guessed_from_system_ids_prove_nothing(self):
        config = _config([], [])
        del config["cores"]
        cats = EntryCategories(config, PROFILES)
        self.assertEqual(
            cats.category({"name": "hardwareconfig.xml"}, "bios/Machines/MSX/hardwareconfig.xml",
                          "microsoft-msx"),
            BIOS)

    def test_a_platform_with_no_core_is_all_firmware(self):
        cats = EntryCategories(_config([], []), PROFILES)
        self.assertEqual(cats.category({"name": "anything.bin"}, "anything.bin", "s"), BIOS)


class Evidence(unittest.TestCase):
    """Each category says what establishes it, so the report documents it."""

    def setUp(self):
        self.cats = EntryCategories(_config(["bluemsx", "devilutionx"], []), PROFILES)

    def test_game_data_names_its_reader_and_reference(self):
        found = self.cats.explain({"name": "diabdat.mpq"}, "ports/diablo/diabdat.mpq", "diablo")
        self.assertEqual(found["category"], GAME_DATA)
        self.assertEqual(found["read_by"], [{
            "emulator": "devilutionx", "proof": "name",
            "entries": [{"name": "diabdat.mpq", "source_ref": "Source/init.cpp:120"}]}])
        self.assertEqual(verify.category_evidence_text(found),
                         "read by devilutionx: diabdat.mpq (Source/init.cpp:120)")

    def test_unread_names_the_cores_examined(self):
        found = self.cats.explain({"name": "hardwareconfig.xml"},
                                  "bios/Machines/MSX/hardwareconfig.xml", "microsoft-msx")
        self.assertEqual(found, {"category": UNREAD, "cores_examined": ["bluemsx"]})
        self.assertEqual(verify.category_evidence_text(found),
                         "no reader found (cores examined: bluemsx)")

    def test_an_unsettled_entry_says_why(self):
        cats = EntryCategories(_config(["bluemsx", "devilutionx", "mystery"], []), PROFILES)
        found = cats.explain({"name": "hardwareconfig.xml"},
                             "bios/Machines/MSX/hardwareconfig.xml", "microsoft-msx")
        self.assertEqual(found["category"], BIOS)
        self.assertIn("mystery", found["unsettled"])
        self.assertIn("not settled", verify.category_evidence_text(found))


class StrictCountsFirmwareOnly(unittest.TestCase):
    def test_missing_game_data_and_unread_carry_no_severity(self):
        config = _config(["bluemsx", "devilutionx"], [
            {"name": "diabdat.mpq", "destination": "ports/diablo/diabdat.mpq", "required": True},
            {"name": "hardwareconfig.xml",
             "destination": "bios/Machines/MSX/hardwareconfig.xml", "required": True},
            {"name": "MSX.ROM", "destination": "bios/Machines/MSX/MSX.ROM", "required": True},
        ], system="microsoft-msx")
        db = {"files": {}, "indexes": {"by_md5": {}, "by_name": {}, "by_crc32": {},
                                       "by_path_suffix": {}}}
        result = verify.verify_platform(config, db, emu_profiles=PROFILES)
        self.assertEqual(result["severity_counts"][verify.Severity.CRITICAL], 1)
        self.assertEqual(result["missing_not_firmware"], {
            "game_data": ["ports/diablo/diabdat.mpq"],
            "unread": ["bios/Machines/MSX/hardwareconfig.xml"],
        })
        self.assertEqual(result["status_counts"][verify.Status.MISSING], 3)
        evidence = {d["name"]: d.get("category_evidence") for d in result["details"]}
        self.assertEqual(evidence["hardwareconfig.xml"]["cores_examined"], ["bluemsx"])
        self.assertEqual(evidence["diabdat.mpq"]["read_by"][0]["emulator"], "devilutionx")


if __name__ == "__main__":
    unittest.main()
