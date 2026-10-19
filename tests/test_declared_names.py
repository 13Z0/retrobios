"""The command line report judges "undeclared" on the flat declared set.

Per normalized system id, bk and elektronika-bk never met, nor nintendo-sgb
and nintendo-super-game-boy: 646 entries whose exact name a platform
declares were counted undeclared, while verify and the site read one flat
set, enriched with every spelling the database knows.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from cross_reference import (  # noqa: E402
    cross_reference,
    declared_names,
    load_platform_files,
)


class FlatDeclaredSet(unittest.TestCase):
    def test_a_name_declared_under_another_system_id_is_not_a_gap(self):
        """A platform files the Super Game Boy BIOS under nintendo-sgb, the
        profile under nintendo-super-game-boy: the ids never meet."""
        import tempfile  # noqa: PLC0415

        with tempfile.TemporaryDirectory(dir=REPO_ROOT / "tmp") as tmp:
            platforms = Path(tmp)
            (platforms / "fx.yml").write_text(
                "platform: Fx\nverification_mode: existence\nsystems:\n"
                "  nintendo-sgb:\n    files:\n    - name: sgb.boot.rom\n"
                "      destination: sgb.boot.rom\n"
            )
            profiles = {"sgbcore": {
                "emulator": "SGB", "type": "libretro", "systems": ["nintendo-super-game-boy"],
                "files": [{"name": "sgb.boot.rom", "system": "nintendo-super-game-boy"}],
            }}
            db = {"files": {}, "indexes": {"by_name": {}, "by_md5": {}, "by_crc32": {},
                                           "by_path_suffix": {}}}
            declared, data_dirs = load_platform_files(str(platforms), ["fx"])
            per_system = cross_reference(profiles, declared, db, data_dirs, set())
            flat = cross_reference(
                profiles, declared, db, data_dirs, set(),
                all_declared=declared_names(str(platforms), ["fx"], db),
            )
        self.assertEqual([g["name"] for g in per_system["sgbcore"]["gap_details"]],
                         ["sgb.boot.rom"])
        self.assertEqual(flat["sgbcore"]["gap_details"], [])


class AnAliasIsADeclaration(unittest.TestCase):
    """RetroArch lists amiberry's cd32.rom as kick40060.CD32, an alias the
    profile declares, yet the gap report still called cd32.rom undeclared."""

    def test_a_file_declared_under_its_alias_is_covered(self):
        profiles = {"amiberry": {
            "emulator": "Amiberry", "type": "libretro", "systems": ["commodore-amiga"],
            "files": [{"name": "cd32.rom", "aliases": ["kick40060.CD32"], "size": 524288}],
        }}
        db = {"files": {}, "indexes": {"by_name": {}, "by_md5": {}, "by_crc32": {},
                                        "by_path_suffix": {}}}
        report = cross_reference(
            profiles, {}, db, {}, {"by_name": {}},
            all_declared={"kick40060.CD32"},
        )
        self.assertEqual(report["amiberry"]["gap_details"], [])

if __name__ == "__main__":
    unittest.main()
