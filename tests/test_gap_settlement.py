"""The gap report settles a core file as verify does: slot or size.

cross_reference counted a core file declared as soon as a platform named
it. RetroArch names Galaksija's 4 KiB galaksija/rom1.bin, so DOSBox's
32 KiB SC-55/ROM1.BIN left the gaps page while verify listed it undeclared.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from cross_reference import cross_reference  # noqa: E402
from validation import declaration_settles  # noqa: E402

SC55 = {"name": "ROM1.BIN", "path": "SC-55/ROM1.BIN", "size": 32768,
        "validation": ["size"], "required": True}


class OneRule(unittest.TestCase):
    def test_a_same_named_file_of_another_size_does_not_settle(self):
        declared = {"ROM1.BIN": [(4096, "galaksija/rom1.bin")]}
        self.assertFalse(declaration_settles(SC55, "ROM1.BIN", declared, SC55["path"]))
        declared["ROM1.BIN"].append((32768, "rom1.bin"))
        self.assertTrue(declaration_settles(SC55, "ROM1.BIN", declared, SC55["path"]))

    def test_the_gap_report_reads_it(self):
        profiles = {"dosbox": {"emulator": "DOSBox", "type": "libretro",
                               "systems": ["dos"], "files": [SC55]}}
        report = cross_reference(
            profiles, {}, {"files": {}, "indexes": {}},
            all_declared={"ROM1.BIN": [(4096, "galaksija/rom1.bin")]},
        )
        self.assertEqual(
            [g["name"] for g in report["dosbox"]["gap_details"]], ["ROM1.BIN"]
        )

    def test_both_sides_call_it(self):
        for module in ("verify.py", "cross_reference.py"):
            source = (REPO_ROOT / "scripts" / module).read_text(encoding="utf-8")
            self.assertIn("declaration_settles(", source, module)


class TheRunningBuildPlacesTheGap(unittest.TestCase):
    """Batocera runs Cemu standalone, which reads keys.txt at its
    standalone_path. The gap report gave the libretro Cemu/keys.txt while
    verify gave keys.txt: 163 entries told apart, and the declaration was
    settled on a slot the platform never fills."""

    KEYS = {"name": "keys.txt", "path": "Cemu/keys.txt",
            "standalone_path": "keys.txt", "required": True}

    def _gap_paths(self, standalone: set[str]) -> list[str]:
        profiles = {"cemu": {"emulator": "Cemu", "type": "standalone + libretro",
                             "systems": ["nintendo-wiiu"], "files": [self.KEYS]}}
        report = cross_reference(
            profiles, {}, {"files": {}, "indexes": {}}, all_declared={},
            standalone_cores=standalone,
        )
        return [g["path"] for g in report["cemu"]["gap_details"]]

    def test_a_standalone_build_reads_its_own_path(self):
        self.assertEqual(self._gap_paths({"cemu"}), ["keys.txt"])

    def test_the_core_reads_the_libretro_path(self):
        self.assertEqual(self._gap_paths(set()), ["Cemu/keys.txt"])


if __name__ == "__main__":
    unittest.main()
