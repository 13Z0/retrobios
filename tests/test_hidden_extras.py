"""A core file the builder holds but cannot place is not counted packed.

The builder skips a core extra whose destination a case-insensitive
filesystem folds onto a packed file, or that a packed file blocks as a
directory. verify counted both in "Core files: N in pack": RetroPie's
openmsx fmpac.rom (the pack carries the platform's FMPAC.ROM, other bytes),
ROCKNIX's higan SGB1.sfc/program.rom under the file SGB1.sfc.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from packpaths import placement_block  # noqa: E402


class OneRule(unittest.TestCase):
    def test_reasons(self):
        seen = {"bios/FMPAC.ROM", "bios/SGB1.sfc"}
        lower = {d.lower() for d in seen}
        parents = {"bios"}
        cases = {
            "bios/FMPAC.ROM": "taken",
            "bios/fmpac.rom": "case_twin",
            "bios/SGB1.sfc/program.rom": "path_conflict",
            "bios/other.rom": None,
        }
        for dest, reason in cases.items():
            with self.subTest(dest=dest):
                self.assertEqual(
                    placement_block(dest, seen, lower, parents, True), reason
                )
        self.assertIsNone(
            placement_block("bios/fmpac.rom", seen, lower, parents, False)
        )

    def test_builder_and_report_read_it(self):
        for module in ("generate_pack.py", "packextras.py"):
            source = (REPO_ROOT / "scripts" / module).read_text(encoding="utf-8")
            self.assertIn("placement_block(", source, module)
        builder = (REPO_ROOT / "scripts" / "generate_pack.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("if full_dest.lower() in seen_lower and case_insensitive", builder)


class TheReportSaysSo(unittest.TestCase):
    """Reads the repository: skips when the database is absent."""

    @classmethod
    def setUpClass(cls):
        if not (REPO_ROOT / "database.json").exists():
            raise unittest.SkipTest("database.json is not built")
        previous = os.getcwd()
        os.chdir(REPO_ROOT)
        try:
            from common import load_database, load_emulator_profiles
            from verify import verify_platform
            from common import load_platform_config

            db = load_database("database.json")
            profiles = load_emulator_profiles("emulators")
            config = load_platform_config("retropie", "platforms")
            cls.result = verify_platform(config, db, "emulators", profiles)
        finally:
            os.chdir(previous)

    def _entry(self, emulator: str, path: str) -> dict:
        for u in self.result["undeclared_files"]:
            if u.get("emulator") == emulator and (u.get("path") or u.get("name")) == path:
                return u
        self.skipTest(f"{emulator} {path} is not in the cross-reference")

    def test_a_case_twin_with_other_bytes_is_left_out(self):
        entry = self._entry("openMSX", "fmpac.rom")
        self.assertIs(entry.get("in_pack"), False)
        self.assertIn("another case", entry.get("omitted", ""))

    def test_a_twin_with_the_same_bytes_serves_the_core(self):
        entry = self._entry("Stella 2023", "KVS1.WAV")
        self.assertIsNot(entry.get("in_pack"), False)

    def test_a_directory_under_a_packed_file_is_left_out(self):
        entry = self._entry("nSide (SFC Balanced)", "SGB1.sfc/lr35902.boot.rom")
        self.assertIs(entry.get("in_pack"), False)


if __name__ == "__main__":
    unittest.main()
