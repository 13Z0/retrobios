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


if __name__ == "__main__":
    unittest.main()
